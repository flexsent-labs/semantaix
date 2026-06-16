# Story 16.08 — Operator Customer Chat Channel (Inbound + Outbound)

## Objective

Wire each operator's linked Telegram user account as their **customer-facing chat line**: clients message that account directly; the platform answers and delivers HITL replies **through the same account** via Telethon (not the Semantaix bot).

**As an** operator,
**I want** customers who DM my linked Telegram account to receive AI answers and my HITL replies on that same account,
**So that** my business line on Telegram is my personal/user account — not the platform bot.

PRD: **FR-16-13**, **FR-16-14**, **FR-16-15**, **NFR-16-07**.

## Scope

### In Scope

#### Channel model

- **`operator_telegram_auth`** gains columns (migration in this story):
  - `linked_username TEXT` — Telegram username of the linked user account (from Telethon `get_me()` after auth).
  - `customer_channel_active INTEGER NOT NULL DEFAULT 0` — set `1` when session authenticated + listener started.

- **Conversation channel tagging** — extend inbound seam:
  - `InboundMessageRequest` adds optional fields:
    - `delivery_channel: Literal["bot", "operator_user"]` (default `"bot"`).
    - `operator_id: int | None` — required when `delivery_channel == "operator_user"`.
  - `AnswerContext` / trace metadata records `delivery_channel` + `operator_id` for replay and HITL ticket routing.
  - HITL tickets created from operator-user-channel inbound store `delivery_channel` + `operator_id` on the ticket row (additive column migration in `semantaix_hitl.db`).

#### `user_gateway` — per-operator listeners

- **`OperatorClientPool`** — manages one `TelegramClient` + `MessageRouter` per authenticated operator:
  - On auth success (16-06 hook): start client from operator session file, register `NewMessage` handler, start queue drain + reconnect watchdog (Epic 15.03 pattern).
  - On pool shutdown / operator deactivate: disconnect client.

- **`MessageRouter` per operator**:
  - Private DMs only (`event.is_private`).
  - Drop messages where sender is the linked operator themselves (operator testing).
  - Forward to `POST /conversations/inbound` with:
    ```json
    {
      "chat_id": <customer_chat_id>,
      "text": "...",
      "customer_username": "...",
      "delivery_channel": "operator_user",
      "operator_id": <id>,
      "trace_id": "..."
    }
    ```

- **`POST /messages/send`** (internal, Bearer token):
  - Body: `{operator_id, chat_id, text}`.
  - Uses that operator's Telethon client: `await client.send_message(chat_id, text)`.
  - 404 if operator not linked / client not connected.
  - Rate-limit / flood-wait handling via `flood_sleep_threshold` (NFR-15-02).

#### `api` — outbound routing

- **`OutboundDeliveryRouter`** (or extend `_safe_send_message`):
  - When `delivery_channel == "bot"` (default): existing `telegram_bot_sender.send_message`.
  - When `delivery_channel == "operator_user"`: `httpx` POST to `user_gateway /messages/send` with `operator_id` from context/ticket.
  - Applies to: pipeline answers, HITL ack, HITL operator reply delivery (`/hitl/tickets/{id}/reply`).

- **`UserGatewayClient`** thin wrapper in api (mirrors bot's pattern): base URL from `settings.user_gateway_base_url`.

#### Onboarding copy (16-05 amendment)

- Telegram link button success DM:
  `"✓ Аккаунт привязан. Клиенты могут писать вам в личные сообщения Telegram — ответы будут приходить с этого аккаунта."`

### Out of Scope

- Group / channel messages on operator account (private DMs only, same as Epic 15).
- Operator sending manual messages from the platform UI (only automated pipeline + HITL reply path).
- Migrating existing bot-only customers to operator account mid-conversation.

## Implementation Notes

- **Two Telegram surfaces per operator:**
  | Surface | Account | Purpose |
  |---------|---------|---------|
  | Platform bot | Semantaix bot | `/register`, onboarding, admin, operator commands |
  | Customer line | Operator's linked user account | Client DMs in + AI/HITL out |

- **NFR-15-05 exception:** Epic 15 "receive-only" applies to unsolicited automation. Operator-channel **replies to customers who messaged first** are in scope (FR-16-15). No cold outreach.

- **Project resolution:** `operator_id` → `operators.project_id` for RAG scoping (Epic 10), same as bot path.

- **HITL operator notify:** Still DMs the operator on the **platform bot** (their `operators.chat_id` from registration) — unchanged.

- **Idempotency:** Reuse trace_id pattern; include `delivery_channel` in idempotency key scope if needed.

## Test Plan

### Unit

- `tests/user_gateway/test_operator_client_pool.py` — start/stop per operator; forward payload shape.
- `tests/user_gateway/test_operator_send_message.py` — send via mock client; 404 when not connected.
- `tests/test_api_outbound_delivery_router.py` — bot vs operator_user routing branches.
- `tests/test_inbound_operator_channel.py` — inbound with `delivery_channel=operator_user` sets project from operator.

### Integration

- Full loop mock: customer DM on operator client → api inbound → pipeline answer → user_gateway send.

## Automated E2E Verification

`tests/e2e/test_e2e_epic16_operator_customer_channel.py` in 16-07 signoff scope.

## Manual Verification

1. Operator links Telegram account via onboarding QR.
2. Customer DMs operator's **user account** (not the bot) → receives AI answer from that account.
3. Escalation → operator replies via HITL → customer receives reply on operator user account.

## Done Criteria

- 100% coverage on new modules.
- FR-16-13/14/15 satisfied.
- Onboarding copy reflects customer-line purpose.
