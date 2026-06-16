# Story 16.03 — Callback Query Dispatcher

## Objective

Enable `bot_gateway` to process Telegram `callback_query` updates (inline keyboard button taps). Today these updates are silently ignored in `normalize_update`.

**As a** platform engineer,
**I want** a callback-query normalization and dispatch layer,
**So that** inline buttons for admin approval and operator onboarding can be handled in later stories.

PRD: **FR-16-08**, **NFR-16-03**.

## Scope

### In Scope

- **`services/bot_gateway/app/telegram_update.py`**:
  - New `NormalizedCallbackQuery` frozen dataclass: `update_id, callback_query_id, chat_id, sender_username, sender_user_id, data: str`.
  - New `normalize_callback_query(payload) -> NormalizedCallbackQuery | None`.
  - Keep `normalize_update` unchanged for messages; add separate entry point used by main processor.

- **`services/bot_gateway/app/callback_dispatch.py`**:
  - `async def dispatch_callback_query(normalized, *, handlers) -> dict[str, str] | None`
  - Always calls Telegram `answerCallbackQuery` via `TelegramBotSender` (new method `answer_callback_query(callback_query_id, text?)`) to clear client spinner.
  - Prefix router: `data.split(":", 2)` → `(namespace, action, arg)`.
  - Namespaces registered in later stories: `op_reg` (16-04), `onboard` (16-05). This story wires stub handlers that log `callback_unhandled` and answer with empty ack.

- **`services/bot_gateway/app/main.py`** — `_process_telegram_update`:
  - If payload has `callback_query`, branch before message normalization.
  - Return `{"status": "ok"}` after dispatch.

- **`services/api/app/telegram_bot_sender.py`** (or bot-local sender): `answer_callback_query` wrapping Bot API `answerCallbackQuery`.

### Out of Scope

- Actual approve/reject/onboarding handlers (16-04, 16-05).
- `editMessageReplyMarkup` to disable buttons after tap (nice-to-have in 16-04).

## Implementation Notes

- Callback `data` must be ≤ 64 bytes (NFR-16-03). Document convention in module docstring.
- Security: dispatch layer does NOT enforce auth — handlers do.
- Fixture `update_callback_query_valid.json` already exists; extend with `message` sub-object if inline keyboard attached to DM (optional for tests).
- Idempotency: duplicate callback_query ids logged at DEBUG, still answer ack.

## Test Plan

### Unit

- `tests/test_telegram_callback_normalize.py` — valid callback → NormalizedCallbackQuery; missing fields → None or validation error.
- `tests/test_bot_gateway_callback_dispatch.py` — dispatches to stub; answerCallbackQuery always called; unknown namespace logged.

### Regression

- `tests/test_bot_gateway_webhook.py` — update `test_webhook_ignores_callback_query_update` → now processes callback (expect 200 + answer call).

## Automated E2E Verification

Deferred to 16-07.

## Manual Verification

1. Send test inline keyboard via Bot API script → tap → server logs dispatch + Telegram spinner clears.

## Done Criteria

- 100% coverage on new modules.
- Existing message webhook tests still green.
