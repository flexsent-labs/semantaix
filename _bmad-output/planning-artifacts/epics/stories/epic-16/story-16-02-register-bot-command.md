# Story 16.02 — `/register` Bot Command

## Objective

Give prospective operators a self-service Telegram entry point that creates a registration request via the api and confirms submission in Russian.

**As a** prospective operator,
**I want** to type `/register` in the bot,
**So that** I can apply to join the platform without waiting for an admin to add me manually.

PRD: **FR-16-01**, **FR-16-11**.

## Scope

### In Scope

- **`services/bot_gateway/app/operator_registration_commands.py`**:
  - Regex: `^/register(?:@\w+)?(?:\s+(.+))?$` (case-insensitive), start-of-message anchored.
  - Gate: sender NOT in operator registry (`operator_resolver` returns None).
  - Call `ApiClient.create_operator_register_request(username, chat_id, display_name)` → `POST /operators/register-request`.
  - Success DM: `"Заявка отправлена. Администратор получит уведомление."` (platform bot is **@semantaix_bot** / Semantaix).
  - Error mapping:
    - `registration_pending` → `"У вас уже есть активная заявка."`
    - `registration_cooldown` → `"Заявка была отклонена. Повторная подача возможна через 24 часа."`
    - `already_operator` (local gate) → `"Вы уже зарегистрированы как оператор."`
    - api unreachable → `"Не удалось отправить заявку. Попробуйте позже."`

- **`ApiClient`** method `create_operator_register_request`.

- Wire dispatcher in `bot_gateway/app/main.py` alongside other slash commands.

- **Admin notification hook** (minimal in this story): after successful create, call new api internal endpoint `POST /operators/register-request/{id}/notify-admin` OR have api fire `telegram_bot_sender` directly on create — **prefer api-side notify** so bot_gateway stays thin. Api looks up admin chat_id via `operator_repository.find_by_username(settings.admin_telegram_username)` and sends DM with plain text only (buttons added in 16-04). Message: `"Новая заявка оператора: {username}, chat_id={chat_id}, имя: {display_name or '—'}. Кнопки подтверждения — в story 16-04."`

### Out of Scope

- Inline approve/reject buttons (16-04).
- Onboarding DM (16-05).

## Implementation Notes

- `display_name` captured from optional trailing text after `/register`.
- Log `operator_register_requested` with username + request_id; never log chat_id in INFO (DEBUG ok).
- Unauthorized path (already operator): no api call, immediate DM.

## Test Plan

### Unit

- `tests/test_bot_gateway_register_command.py` — regex cases; operator gate; api success/error DMs; display_name parsing.

### Integration

- Fake ApiClient + fake send_dm capture.

## Automated E2E Verification

Deferred to 16-07.

## Manual Verification

1. Unknown user `/register Иван` → confirmation DM; admin receives plain notification.
2. Same user `/register` again → "активная заявка".
3. Registered operator `/register` → "уже зарегистрированы".

## Done Criteria

- 100% coverage on `operator_registration_commands.py` + new `ApiClient` method.
- Admin receives notification DM on new request.
