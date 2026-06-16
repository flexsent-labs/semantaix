# Story 16.04 — Admin Approval Inline Buttons

## Objective

Replace the plain-text admin notification from 16-02 with an inline keyboard so the admin can approve or reject operator registration requests with one tap.

**As a** platform admin,
**I want** Approve and Reject buttons on registration notification DMs,
**So that** I can onboard operators without opening the web UI or typing commands.

PRD: **FR-16-02**, **FR-16-03**, **FR-16-04**, **FR-16-08**.

## Scope

### In Scope

- **Admin notification DM** (api `notify-admin` on request create, or refactored from 16-02):
  - Russian body with applicant details.
  - Inline keyboard:
    ```json
    {"inline_keyboard": [[
      {"text": "✓ Одобрить", "callback_data": "op_reg:approve:<request_id>"},
      {"text": "✗ Отклонить", "callback_data": "op_reg:reject:<request_id>"}
    ]]}
    ```
  - Send via `TelegramBotSender.send_message(..., reply_markup=...)`.

- **`services/bot_gateway/app/operator_registration_callbacks.py`**:
  - Handler for namespace `op_reg`:
    - `approve:<id>` — verify sender == `settings.admin_telegram_username` (or admin resolver). Call `ApiClient.approve_operator_register_request(id)`. On success:
      - Edit original message markup to remove buttons (`editMessageReplyMarkup`).
      - DM applicant: `"✓ Вы зарегистрированы как оператор."` (triggers 16-05 onboarding in same PR or follow-up — **this story calls internal `POST /operators/register-requests/{id}/onboarding-notify`** added in 16-05 if not ready; stub DM ok for 16-04 AC).
      - Answer callback: `"Одобрено"`.
    - `reject:<id>` — same admin gate. Call reject endpoint. DM applicant rejection text. Answer callback: `"Отклонено"`.
  - Non-admin tap → ignore + log `unauthorized_op_reg_callback` + answer empty.

- **`ApiClient`**: `approve_operator_register_request`, `reject_operator_register_request` (admin token path: internal bearer + `as_user=admin` OR dedicated internal approve with HMAC — **use existing `require_admin_or_internal` pattern**).

### Out of Scope

- Onboarding buttons (16-05).
- Project picker on approve (always default project in v1).

## Implementation Notes

- `request_id` in callback_data is decimal string; validate `int` parse.
- Race: two admins tap Approve — second gets 409/idempotent safe response; edit markup anyway.
- After reject, set `rejection_cooldown_until` (repository from 16-01).

## Test Plan

### Unit

- `tests/test_operator_registration_callbacks.py` — admin approve/reject; unauthorized ignored; api error surfaces answer text.

### Contract

- Approve creates operator + updates request status atomically (repository test from 16-01).

## Automated E2E Verification

`tests/e2e/test_e2e_epic16_admin_approval.py` (stub in 16-07 if not here):
- Simulate callback_query webhook with `op_reg:approve:1` from admin user → operator row exists.

## Manual Verification

1. User `/register` → admin DM has buttons.
2. Admin taps Approve → applicant DM + buttons removed from admin message.
3. Non-admin taps → no effect.

## Done Criteria

- 100% coverage on callback handlers + send_message reply_markup path.
- FR-16-02/03/04 satisfied.
