# Story 15.03 — Message Routing + Resilience

## Objective

Wire the Telethon `NewMessage` event handler to receive private DMs, enqueue them on an `asyncio.Queue`, and drain the queue into the `api` `/conversations/inbound` endpoint. Add a reconnect watchdog with exponential backoff so the service self-heals after network interruptions without operator intervention.

**As a** platform operator,
**I want** customer DMs received on the user account to automatically reach the answer pipeline,
**So that** customers who message the user account directly receive the same AI-powered responses as bot users.

PRD reference: **FR-15-08** (NewMessage forwarding), **FR-15-09** (operator message filter), **FR-15-10** (asyncio.Queue), **FR-15-11** (reconnect watchdog), **NFR-15-01** (100% coverage), **NFR-15-07** (code conventions).

## Scope

### In Scope

- **`services/user_gateway/app/message_router.py`** — all message routing logic:
  - `MessageRouter` class, constructor-injected `ApiClient`, `InboundRateLimitRepository` (stub — filters added in 15.04), `queue: asyncio.Queue`, `operator_username: str`.
  - `async def handle_new_message(event) -> None`:
    - Guard: `if not event.is_private: return` (only private DMs).
    - Guard: operator filter — if sender username matches `operator_username`, drop silently (FR-15-09).
    - Spam filter hook (stub for 15.04): `if await self._is_spam(event): return`.
    - Enqueue: `self.queue.put_nowait(message)` — on `asyncio.QueueFull`: log WARNING `"user_gateway_queue_full"` with `sender_id`, drop message.
  - `async def _drain_queue(self) -> None`:
    - `while True: message = await self.queue.get(); await self._forward(message)`.
  - `async def _forward(self, message) -> None`:
    - POST `api /conversations/inbound` via `ApiClient` with `chat_id`, `text` (truncated in 15.04), `username`.
    - On HTTP error: log WARNING, message is lost (fire-and-forget; HITL escalation handles unanswered customers).
  - `_is_spam(event) -> bool`: returns `False` for now (filters wired in 15.04).

- **`services/user_gateway/app/watchdog.py`** — reconnect watchdog:
  - `async def run_watchdog(client: TelegramClient, on_connected: Callable) -> None`:
    - `delay = 1`; `while True`:
      - `try: await client.run_until_disconnected(); except Exception: pass`
      - `await asyncio.sleep(delay)`
      - `delay = min(delay * 2, 60)`
      - `await client.connect()`
      - `on_connected()` (resets delay to 1)
  - Watchdog only started when `_state.phase == 'authenticated'` (checked at start; reconnect path does not check auth state since the session file is still valid).

- **`app/main.py` lifespan** extended (builds on 15.01/15.02 stub):
  - On authenticated state detected (either from startup with existing session or after auth flow):
    - Register `handler = client.on(events.NewMessage)(router.handle_new_message)`.
    - `asyncio.create_task(router._drain_queue())`.
    - `asyncio.create_task(run_watchdog(client, on_connected=lambda: None))`.
  - The watchdog and drain tasks are stored on the lifespan's task set to keep them alive.

- **`app/api_client_user_gateway.py`** — `ApiClientUserGateway` (thin wrapper, constructor-injected `base_url` and `internal_service_token`; mirrors `services/bot_gateway/app/api_client.py`):
  - `async def forward_inbound(self, *, chat_id: int, text: str, username: str | None) -> None`.
  - Uses `httpx.AsyncClient`; raises on non-2xx.

- **`asyncio.Queue(maxsize=100)`** — instantiated in `app/main.py`, injected into `MessageRouter`.

### Out of Scope

- Spam filter implementation (stubs only in this story; 15.04 fills them in).
- Length truncation (15.04).
- Rate limiting (15.04).
- QR auth flow (15.02 owns it).

## Implementation Notes

- **Queue sizing**: `maxsize=100` is chosen so the queue can absorb a burst of ~100 messages during a temporary API unavailability without growing unbounded. At normal rates (<<100 msg/min), the queue depth stays near 0.
- **Fire-and-forget `_forward`**: Matching the existing `bot_gateway` pattern — `api` creates a HITL ticket and acks to the customer if it can't answer. Retrying failed forwards here would duplicate messages. Log and discard on error.
- **`event.is_private` check**: Telethon's `events.NewMessage` fires for all message types. `event.is_private` is True only for private 1-on-1 conversations (not groups, channels, or supergroups). This is the correct boundary.
- **Operator filter**: Compare `event.sender.username` (lowercased, strip `@`) against `settings.hitl_primary_operator_username` (same normalization). If the account running `user_gateway` IS the operator account, this guard prevents the operator's own messages to themselves from being forwarded. More importantly, if the operator sends a test message to the user account from their own account, it is dropped here.
- **Watchdog backoff**: 1→2→4→8→16→32→60 (capped). On reconnect, reset to 1. This is intentionally simple — no jitter needed for a single-client scenario.
- **Task lifecycle**: Use `asyncio.create_task()` and keep a reference (e.g., `_tasks: set[asyncio.Task]` in lifespan) to prevent garbage collection of background tasks.
- **`ApiClientUserGateway` vs `ApiClient`**: The `bot_gateway` `ApiClient` is a reasonable model but lives in a different service. Create a new thin client in `user_gateway` rather than importing cross-service. Both call the same `api` endpoints.
- **`MemorySession` + SimpleNamespace fake events** for all tests: no real Telegram connections.

## Test Plan

### Unit

- `tests/user_gateway/test_message_router.py`:
  - Non-private event (`event.is_private = False`) → `handle_new_message` returns without enqueuing.
  - Operator username match → silent drop.
  - Valid customer message → enqueued.
  - `asyncio.QueueFull` (maxsize=0 queue) → WARNING logged, message dropped, no exception raised.
  - `_drain_queue`: mock `_forward` to capture calls; confirm message dequeued and forwarded.
  - `_forward` HTTP error → logged, no exception propagated.

- `tests/user_gateway/test_watchdog.py`:
  - Normal run: `run_until_disconnected()` completes → client reconnects after backoff.
  - Exception from `run_until_disconnected()` → caught, client reconnects.
  - Backoff doubles each iteration up to 60s max; verify sequence `[1, 2, 4, 8, 16, 32, 60, 60]` by mocking `asyncio.sleep`.
  - `on_connected()` called after successful reconnect.

- `tests/user_gateway/test_api_client_user_gateway.py`:
  - `forward_inbound` sends correct JSON body to `/conversations/inbound`.
  - Non-2xx response raises `httpx.HTTPStatusError`.

### Contract

- `forward_inbound` payload: `{"chat_id": int, "text": str, "username": str | None}` — match existing `bot_gateway` ApiClient `forward_inbound` shape.

### Integration

- `tests/user_gateway/test_message_routing_integration.py`:
  - Fake Telethon client fires a NewMessage event → `MessageRouter` processes it → captured by `respx` mock of `api` → assert correct POST body.
  - Queue drain: 3 messages enqueued → all 3 forwarded in order.

## Automated E2E Verification

- Covered by integration tests with mock Telethon and `respx` api mock.

## Manual Verification

1. Send a DM to the user account from a non-operator account → `api` receives the message (verify via `api` logs or HITL ticket creation).
2. Send a DM from the operator account → confirm it does NOT appear in `api` logs.
3. Kill network for 10s, restore → `user_gateway` reconnects automatically (observe watchdog backoff in logs).
