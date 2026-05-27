# Story 14.03 — Message-volume instrumentation (bot_gateway)

## Objective
Wire the `bot_gateway` message branches to record one row in `usage_messages` per inbound and outbound message via the `UsageRecorder` seam (built in 14.02). Zero-LLM messages (customer "спасибо" that hits no answerer, bot ack messages) still count toward the **message tracker** but never toward the LLM tracker — this is the load-bearing separation between volume and spend.

**As an** admin or operator,
**I want** to see total customer / operator message volume per project per day independent of LLM activity,
**So that** I can spot traffic spikes that don't correlate with cost spikes (and vice versa), and reason about engagement separately from spend.

PRD reference: **FR-29** (Message-Volume Capture).

## Scope

### In Scope
- **`UsageMessageRepository.record(*, project_id, direction, participant_role, trace_id, created_at)`** implementation in `services/api/app/usage/repositories.py` (replacing the 14.01 skeleton):
  - Validates `direction ∈ {'in','out'}` and `participant_role ∈ {'customer','operator'}` at the Python boundary (in addition to the SQL CHECK constraint).
  - INSERTs one row; `trace_id` may be NULL.
- **bot_gateway inbound instrumentation** in `services/bot_gateway/app/main.py` (or the webhook handler module):
  - On every accepted Telegram update where a message is present (customer or operator sender), after the message is persisted to `semantaix_story1.db`, call `UsageRecorder.record(tracker_type='messages', project_id=<resolved>, payload={direction='in', participant_role=<customer|operator>, created_at=<now>}, trace_id=<from-context-or-none>)`.
  - **`project_id` resolution**: matches the existing inbound-routing path — for customer messages, project resolved via `conversations.project_id` from the persisted message; for operator messages, project resolved via the operator's registered project (Epic 10). When the resolution fails (e.g. unknown sender), the row is NOT recorded — silent skip with a `usage_message_skipped_no_project` debug log (rare, no SLA impact).
  - **`participant_role`** = `customer` when the sender is not a registered operator/admin; `operator` when the sender matches the operator-registry username.
- **bot_gateway outbound instrumentation** in `services/bot_gateway/app/telegram_sender.py` (or whatever module owns `TelegramBotSender`):
  - On every successful outbound `sendMessage` (or media-message) to a customer chat, call `UsageRecorder.record(tracker_type='messages', project_id=<resolved>, payload={direction='out', participant_role='operator', created_at=<now>}, trace_id=<from-context-or-none>)`.
  - **Note** that all outbound is `participant_role='operator'` because the bot delivers operator-side messages (whether they originate from the operator, the answerer, or a system ack — they're all on the operator side of the conversation by the load-bearing model).
  - Failed outbound (Telegram returned 4xx/5xx) is NOT recorded — only successful deliveries count.
- **UsageRecorder access from bot_gateway** — the bot_gateway service does NOT write to `semantaix_usage.db` directly. It calls the api's existing `internal_service_token`-authenticated endpoint surface: a new `POST /api/usage/record` endpoint owned by the api routes the payload to the api's `UsageRecorder` instance.
  - **New api endpoint `POST /api/usage/record`** behind `internal_service_token` (Bearer); body `{tracker_type, project_id, payload, trace_id?}`; returns `202 Accepted` immediately after enqueue. **No content** in response body.
  - **`ApiClient.record_usage(tracker_type, project_id, payload, trace_id)`** new method in `services/bot_gateway/app/api_client.py` — async, fire-and-forget (calls the endpoint without `await`-ing the result for the critical message-routing path); uses `asyncio.create_task` to dispatch in the background and logs `usage_record_dispatch_failed` on httpx failure (never raises into the bot's message handler).
- **Critical-path liveness** — the bot's inbound webhook handler and outbound sender MUST NOT block on the usage record. NFR-8 binding: if the api is unreachable or `usage.db` is unavailable, the customer message is still delivered / persisted / processed normally; the usage row is silently dropped.

### Out of Scope
- HITL event instrumentation (14.04).
- Daily roll-up (14.05).
- Dashboard, API read endpoints, `/usage` bot command (14.06–14.08).
- Alerting (14.09).
- LLM-call instrumentation (14.02 — already shipped).
- Filtering out specific message types (e.g. command-only messages) — every accepted Telegram message counts, regardless of slash-command vs natural text.
- Cross-service token shape changes — `internal_service_token` already exists.

## Implementation Notes
- **Fire-and-forget from bot_gateway** — `ApiClient.record_usage` uses `asyncio.create_task(self._post_record_usage(...))` and returns immediately. The background task logs httpx failures but does not retry. Matches the brainstorm K2 acceptance of silent loss on transport failure.
- **api `POST /api/usage/record` shape** — accepts the same shape as `UsageRecorder.record`'s `payload` arg. The api endpoint validates `tracker_type`, `project_id`, and the payload shape (per-tracker discriminator), then enqueues onto the recorder. Returns `202 Accepted` (not `200`) to indicate "queued, not durable".
- **`project_id` resolution timing** — for inbound, resolve AFTER the message persists (so `conversations.project_id` exists); for outbound, resolve from the originating ticket/context (the caller passes it in). When resolution fails, skip the row.
- **No `trace_id` for unsolicited outbound** — most outbound is `trace_id`-correlated (it's a reply to an inbound). The few cases without (e.g. a future scheduled-notification path) record `trace_id=NULL`.
- **No participant_role inference at the api seam** — the bot_gateway determines `participant_role` BEFORE dispatch (it's the only service that knows whether the sender is a registered operator). The api seam takes it as-given.
- **Outbound rows are written FROM the bot_gateway, but the api STILL owns the recorder.** The bot_gateway calls `api/usage/record` from a successful outbound-handler hook (after Telegram returns 200 OK).
- **No re-instrumentation of media messages** — one row per Telegram update (text + media count as one outbound from the bot's perspective).
- **Settings field** — no new env vars in this story; `internal_service_token` already exists in Settings.
- **Structured logging** — `usage_message_skipped_no_project` (debug), `usage_record_dispatch_failed` (warning), `usage_record_received` (info — on the api endpoint, with `tracker_type`).

## Test Plan

### Unit
- `tests/test_usage_message_repository.py`:
  - `record(direction='in', participant_role='customer', ...)` inserts a row; round-trip read returns the same values.
  - `record(direction='out', participant_role='operator', ...)` inserts; round-trip OK.
  - `direction='sideways'` or `participant_role='alien'` raises `ValueError` BEFORE touching the DB.
  - `trace_id=None` accepted; stored as NULL.
- `tests/test_api_usage_record_endpoint.py`:
  - `POST /api/usage/record` with valid `internal_service_token` + valid payload → 202; recorder receives one item.
  - Missing token → 401; invalid token → 401.
  - Invalid `tracker_type` → 400; invalid payload shape (e.g. messages payload without `direction`) → 400.
- `tests/test_bot_gateway_inbound_message_instrumentation.py`:
  - Customer inbound → `ApiClient.record_usage` called with `tracker_type='messages', direction='in', participant_role='customer'` and the resolved `project_id`.
  - Operator inbound (sender matches operator registry) → called with `participant_role='operator'`.
  - Unknown sender (project resolution fails) → `record_usage` NOT called; `usage_message_skipped_no_project` debug log appears.
- `tests/test_bot_gateway_outbound_message_instrumentation.py`:
  - Successful outbound to customer chat → `record_usage` called with `direction='out', participant_role='operator'`.
  - Failed outbound (Telegram 502) → `record_usage` NOT called.
  - Bot ack message (HITL "we'll get back to you") → recorded as outbound.
- `tests/test_api_client_record_usage_fire_and_forget.py`:
  - `record_usage` returns within microseconds even when the api is unreachable; background task logs `usage_record_dispatch_failed` on httpx error.
  - Inbound webhook handler completes its return value even when `record_usage` would have failed (NFR-8).

### Contract
- `tests/contract/test_usage_record_endpoint_contract.py` — assert `POST /api/usage/record` accepts the documented shape and returns 202; reject payload variations.

### Integration
- `tests/test_inbound_message_records_usage.py` — boot the api + use the bot_gateway test client (or directly invoke the inbound handler); send a customer text → assert one row in `usage_messages` with `direction='in', participant_role='customer'`.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_message_round_trip.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-03")`):
  - Mock Telegram webhook + outbound Telegram client per the existing harness.
  - Send a customer message → assert one `usage_messages` inbound row; if the answerer produces a reply, assert one `usage_messages` outbound row.
  - Send a customer message that escalates to HITL → assert outbound ack row.
  - Force the api unreachable (mock httpx to raise) → assert no `usage_messages` rows AND no customer-facing latency regression (the bot's message handler returns normally).

## Manual Verification
1. `docker compose up --build -d`; send a Telegram customer message → `sqlite3 .data/semantaix_usage.db "SELECT * FROM usage_messages ORDER BY id DESC LIMIT 5;"` shows the inbound row + the outbound row.
2. Send a message that triggers a HITL escalation → confirm both the inbound row AND the outbound ack row.
3. Stop the api container; send a customer message via Telegram → confirm the bot still delivers a reply (NFR-8); no `usage_messages` rows appear; `usage_record_dispatch_failed` log appears in bot_gateway logs.

## Done Criteria
- 100% line coverage on the new repository method, the new api endpoint handler, the bot_gateway instrumentation diffs, and `ApiClient.record_usage`.
- `ruff check .` passes.
- Fire-and-forget verified — bot_gateway message-handler latency unchanged when api is unreachable (microbenchmark).
- E2E message round-trip green.
- No customer-facing behavior change (Telegram round-trips identical to pre-Epic-14).
