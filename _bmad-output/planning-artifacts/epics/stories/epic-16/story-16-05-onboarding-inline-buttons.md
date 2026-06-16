# Story 16.05 — Post-Approval Onboarding Inline Buttons

## Objective

After admin approval, send the new operator a guided onboarding DM with inline buttons to connect Google Calendar and link the **Telegram user account clients will message** (their customer-facing line — AI and HITL replies go out on that account).

**As a** newly approved operator,
**I want** clear next-step buttons explaining which account clients should message,
**So that** I link the right Telegram identity as my business chat line.

PRD: **FR-16-05**, **FR-16-06**, **FR-16-07**, **FR-16-10**.

## Scope

### In Scope

- **Api hook** `POST /operators/register-requests/{id}/onboarding-notify` (internal, called at end of approve transaction OR from bot after approve):
  - Resolves new `operator_id`.
  - Sends onboarding DM to `operator.chat_id` via `telegram_bot_sender`.
  - Records `onboarding_sent` event.

- **Onboarding DM copy** (Russian):
  ```
  Добро пожаловать! Рекомендуемые шаги настройки:

  1. Подключите Google Calendar — для ответов о свободном времени.
  2. Привяжите Telegram-аккаунт — это линия, на которую будут писать клиенты.
     Ответы клиентам будут приходить с этого аккаунта.

  Нажмите кнопку ниже:
  ```
  Inline keyboard:
  ```json
  {"inline_keyboard": [[
    {"text": "📅 Подключить Google Calendar", "callback_data": "onboard:cal:<operator_id>"},
    {"text": "📱 Привязать Telegram-аккаунт", "callback_data": "onboard:tg:<operator_id>"}
  ]]}
  ```

- **`services/bot_gateway/app/onboarding_callbacks.py`**:
  - Namespace `onboard`:
    - `cal:<operator_id>` — verify tapper is the operator (username match registry). Reuse `calendar_commands.handle_connect_calendar` logic or call shared helper. Record `calendar_started` event via api.
    - `tg:<operator_id>` — verify tapper is owner. If Epic 15.02 not extended yet, DM: `"Сначала дождитесь обновления сервера (story 16-06)."` Otherwise delegate to `start_operator_telegram_link(operator_id)` (16-06). Record `telegram_link_started`.
  - Wrong operator tapping another's buttons → ignore + log `onboarding_callback_owner_mismatch`.

- Wire handlers into `callback_dispatch.py` from 16-03.

### Out of Scope

- Per-operator `user_gateway` session implementation (16-06) — button may stub until 16-06 lands.
- Disabling buttons after completion (v2 polish).

## Implementation Notes

- Calendar path: identical to `/connect_calendar` — `ApiClient.initiate_calendar_connect(project_id, operator_username)`.
- Telegram path: calls `UserGatewayClient.post("/auth/qr_start", params={"operator_id": id})` once 16-06 ships.
- `operator_id` in callback_data fits 64-byte limit for ids up to ~10^15.
- On approve in 16-04, chain to onboarding-notify in same api transaction after operator create.

## Test Plan

### Unit

- `tests/test_onboarding_callbacks.py` — cal button triggers connect flow; tg button calls user_gateway client mock; owner mismatch ignored.
- `tests/test_api_onboarding_notify.py` — sends DM with correct markup.

## Automated E2E Verification

Deferred to 16-07.

## Manual Verification

1. Admin approves registration → operator receives onboarding DM with 2 buttons.
2. Tap calendar → OAuth URL DM arrives.
3. Tap telegram → QR document arrives (after 16-06).

## Done Criteria

- 100% coverage on onboarding callback module + notify endpoint.
- `operator_onboarding_events` rows for `onboarding_sent`, `calendar_started`, `telegram_link_started`.
