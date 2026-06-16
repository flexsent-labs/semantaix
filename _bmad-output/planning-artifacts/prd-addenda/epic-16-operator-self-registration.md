---
title: PRD Addendum — Epic 16 Operator Self-Registration & Onboarding
status: shipped
created: 2026-06-16
parent_prd: _bmad-output/planning-artifacts/PRD.md
epic: epic-16
---

# PRD Addendum — Epic 16: Operator Self-Registration & Onboarding

Amends `PRD.md` with Feature Group **Epic 16**. Backfill into main PRD as a housekeeping PR (same pattern as Epic 15 gap noted in IR report 2026-06-09).

## Feature Group — Operator Self-Registration & Onboarding

### Functional Requirements

- **FR-16-01 Self-Service Registration Request** — A non-operator Telegram user may send `/register [display_name]` to **@semantaix_bot** (the platform bot; display name Semantaix). The bot creates a `pending` registration request keyed by normalized `@username` + `chat_id`. Duplicate pending requests for the same username are rejected with a Russian "already pending" message. Already-registered operators receive "you are already an operator" and no request is created.

- **FR-16-16 Platform Bot Identity** — The Semantaix platform bot uses Telegram username `@semantaix_bot` and display name `Semantaix` (via `bot_persona_*` settings + startup `setMyName`). Configured in `TELEGRAM_PLATFORM_BOT_USERNAME` / `BOT_PERSONA_FIRST_NAME`. Sales/customer LLM persona on operator-linked accounts uses separate `sales_persona_*` settings (human name, default Анна Иванова).

- **FR-16-02 Admin Approval Notification** — On new pending request, the platform admin (`settings.admin_telegram_username` or `hitl_config_admin_username` — use existing admin resolution) receives a Telegram DM containing applicant username, chat_id, display name (if provided), timestamp, and inline **Approve** / **Reject** buttons.

- **FR-16-03 Admin Approval Action** — Tapping **Approve** calls an admin-gated api endpoint that atomically: marks request `approved`, creates an `operators` row (`username`, `chat_id`, `project_id=default`, `display_name`, `is_active=1`), records `reviewed_by` + `reviewed_at`. Returns 409 if username already exists as operator. Idempotent if request already approved.

- **FR-16-04 Admin Rejection Action** — Tapping **Reject** marks request `rejected` with `reviewed_by` + `reviewed_at`. Applicant receives a Russian rejection DM. Rejected username may not re-register for 24 hours.

- **FR-16-05 Post-Approval Onboarding DM** — Immediately after approval, the new operator receives a Russian onboarding DM with inline keyboard buttons: **Подключить Google Calendar** and **Привязать Telegram-аккаунт**. Message explains each step in one sentence. No forced order.

- **FR-16-06 Onboarding — Calendar Button** — Tapping the calendar button triggers the same api + bot flow as `/connect_calendar` for the approved operator (Epic 11 `POST /calendar/connect/initiate`). Operator receives OAuth consent URL DM. Button disabled/hidden after successful calendar token stored (optional v1: always show; operator can reconnect).

- **FR-16-07 Onboarding — Telegram Link Button** — Tapping the Telegram link button starts per-operator QR authentication: `user_gateway POST /auth/qr_start?operator_id=<id>`, bot sends QR as document (Epic 15 pattern), polls `/auth/status`, handles 2FA relay. Session persisted to `.data/operator_sessions/{operator_id}.session`. On success, DM explains this account is where **clients will message the operator** (customer-facing line).

- **FR-16-08 Callback Query Infrastructure** — `bot_gateway` processes `callback_query` updates (currently ignored). Answers callback with `answerCallbackQuery` to clear button spinner. Routes `data` payloads with prefix `op_reg:` (approval) and `onboard:` (onboarding actions). Unauthorized callback senders (non-admin for approval, non-owner for onboarding) are ignored with logged reason.

- **FR-16-09 Registration Request Persistence** — SQLite table `operator_registration_requests` in `semantaix_operators.db` (or dedicated file — see architecture). Columns: `id`, `username`, `chat_id`, `display_name`, `status` (`pending|approved|rejected`), `project_id` (nullable until approval), `created_at`, `reviewed_at`, `reviewed_by`, `rejection_cooldown_until`. Index on `(username, status)`.

- **FR-16-10 Onboarding Event Audit** — Table `operator_onboarding_events`: `operator_id`, `event_type` (`approved|onboarding_sent|calendar_started|calendar_connected|telegram_link_started|telegram_link_connected|customer_channel_active`), `created_at`. Used for metrics and debugging; no PII beyond operator_id.

- **FR-16-11 Internal Registration API** — `POST /operators/register-request` (Bearer `internal_service_token`, called by bot_gateway): body `{username, chat_id, display_name?}`. Returns `{request_id, status}`. `POST /operators/register-request/{id}/approve` and `/reject` (admin session OR internal + `as_admin=`). List pending: `GET /operators/register-requests?status=pending` (admin only).

- **FR-16-12 Per-Operator User Gateway Sessions** — Extends Epic 15 `user_gateway` auth endpoints to accept `operator_id` query param. Each operator has independent auth phase state in `operator_telegram_auth` table and session file on disk. Epic 15 singleton session is **deprecated** for customer traffic — per-operator sessions are the primary model.

- **FR-16-13 Operator Account as Customer Channel** — The Telegram user account linked via QR during onboarding is the operator's **customer-facing chat identity**. Clients initiate conversations by DMing that user account (not the Semantaix platform bot). The platform treats this as the operator's business line.

- **FR-16-14 Operator-Scoped Inbound Routing** — `user_gateway` listens on each authenticated operator's Telethon client for private customer DMs. Forwards to `POST /conversations/inbound` with `delivery_channel=operator_user` and `operator_id`. Project scoping resolves via `operators.project_id`. Operator's own messages on that account are filtered (not forwarded).

- **FR-16-15 Operator-Scoped Outbound Delivery** — AI pipeline answers and HITL replies for conversations on `delivery_channel=operator_user` are sent via `user_gateway POST /messages/send` using that operator's Telethon session (`client.send_message`). Does not use `telegram_bot_sender` (Bot API). Applies to pipeline answers, HITL ack, and operator reply delivery. No unsolicited outbound (reply-only to customers who messaged first).

### Non-Functional Requirements

- **NFR-16-01** — 100% test coverage on all new modules under `platform_common/` and `services/` per `.coveragerc`.
- **NFR-16-02** — Approval and rejection are atomic (single SQLite transaction: update request + create operator on approve).
- **NFR-16-03** — Callback `data` payloads are ≤ 64 bytes (Telegram limit). Use numeric ids: `op_reg:approve:123`, `onboard:cal:45`, `onboard:tg:45`.
- **NFR-16-04** — 2FA passwords and session paths never logged (inherits NFR-15-03/04).
- **NFR-16-05** — Registration cooldown enforced server-side; bot cannot bypass.
- **NFR-16-06** — All user-facing copy Russian-first; admin approval DM may include Latin username as-is.
- **NFR-16-07** — Operator-channel outbound is **reply-only** (customer initiated the DM). No broadcast or cold messaging via linked user accounts. Exception to Epic 15 NFR-15-05 receive-only rule for this scoped reply path only.

### Integration with Epic 02 (Incidents)

- Failed `user_gateway` QR start during onboarding logs `onboarding_telegram_link_failed` at WARNING; does not create incident (operator can retry).
- Repeated approval API failures (5xx from api during callback) log incident fingerprint `operator_registration_approve_failed:<request_id>` at `warning` severity — optional, story 16-07.
