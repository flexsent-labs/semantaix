---
title: Operator Self-Registration & Onboarding
status: draft
created: 2026-06-16
updated: 2026-06-16
author: Aj
---

# Product Brief — Operator Self-Registration & Onboarding

## Problem

Today an operator can only join the platform when an admin manually creates them via the web UI (`/admin/operators`), Telegram admin commands (`/operator_add`), or the admin NL dialog. There is no self-service path: a prospective operator cannot register themselves, and there is no structured onboarding after they are added.

Operators who are approved still need two setup steps before they are productive:

1. Connect their Google Calendar (Epic 11 `/connect_calendar` flow).
The **platform bot** is [@semantaix_bot](https://t.me/semantaix_bot) (display name **Semantaix**) — operators register here via `/register`. The **operator's linked user account** is their customer-facing line (Epic 15 QR + Story 16-08).

Neither onboarding step is offered automatically after registration today.

## Vision

A prospective operator registers through **@semantaix_bot** (display name Semantaix). The platform admin receives an approval request. On approval, the new operator is created in the Epic 10 registry and receives a guided onboarding message with inline action buttons — connect calendar, link Telegram account — reusing proven flows from Epics 11 and 15.

## Target Users

| Persona | Need |
|---------|------|
| **Prospective operator** | Self-register without waiting for admin to discover them; complete setup in-bot |
| **Platform admin** | Review and approve/reject registrations with one tap; no manual data entry for basic cases |
| **Existing operator (re-onboarding)** | N/A in v1 — onboarding DM fires once on first approval only |

## Core User Journeys

### UJ-1 — Self-registration

1. User DMs **@semantaix_bot** `/register` (optional display name).
2. Bot validates: sender is not already a registered operator and has no pending request.
3. Bot confirms: "Заявка отправлена. Администратор получит уведомление."
4. Admin receives DM with applicant details and **Approve** / **Reject** inline buttons.

### UJ-2 — Admin approval

1. Admin taps **Approve** on the registration DM.
2. System creates an `operators` row (username + chat_id from applicant, default project, display name).
3. Applicant receives: "✓ Вы зарегистрированы как оператор."
4. Applicant immediately receives onboarding DM with inline buttons (UJ-3).

### UJ-3 — Post-approval onboarding

1. Operator receives onboarding DM listing recommended next steps.
2. **Подключить Google Calendar** button → bot runs the same flow as `/connect_calendar` (OAuth consent URL DM).
3. **Привязать Telegram-аккаунт** button → bot starts per-operator QR login. Copy explains: *"Это аккаунт, на который будут писать клиенты."*
4. After link: customer DMs that account → platform answers from it; HITL replies deliver on it (Story 16-08).
5. Operator may tap buttons in any order or skip (no forced sequence in v1).

### UJ-4 — Admin rejection

1. Admin taps **Reject**.
2. Applicant receives: "Заявка отклонена. Обратитесь к администратору."
3. Applicant may submit a new `/register` after 24 hours (rate limit).

## Success Metrics

- Time from `/register` to operator row created ≤ 1 admin action (one button tap).
- ≥ 80% of newly approved operators complete at least one onboarding step within 7 days (instrumented via `operator_onboarding_events` table).
- Zero unauthorized operator rows created without admin approval.

## Out of Scope (v1)

- Self-service project selection (admin assigns project on approval; default project when only one exists).
- Web UI registration form (Telegram-only entry point).
- Operator invitation links / referral codes.
- Re-sending onboarding DM on demand (`/onboarding` deferred to v2).
- Replacing admin-created operator paths (admin CRUD remains).

## Dependencies

- **Epic 10** — `operators` registry, admin identity (`admin_telegram_username`).
- **Epic 11** — `/connect_calendar` + OAuth initiate endpoint.
- **Epic 15** — `user_gateway` QR auth (`/auth/qr_start`, `/user_login`); Epic 16 extends this to per-operator sessions.
- **New infrastructure** — `callback_query` handling in `bot_gateway` (currently ignored).

## Open Questions (resolved for v1)

| Question | Decision |
|----------|----------|
| Which project for self-registered operators? | `default` project unless admin picks at approval time (v1: always default; project picker deferred) |
| Can rejected users re-apply immediately? | No — 24 h cooldown per username |
| Per-operator or shared Telegram user session? | Per-operator session files under `.data/operator_sessions/{operator_id}.session` |
| What is the linked account for? | **Customer-facing chat line** — inbound client DMs + outbound AI/HITL replies on that account (not the platform bot) |
| Platform bot vs operator account? | Bot = registration, onboarding, admin, slash commands. Linked user account = client conversations |

## Downstream Artifacts

- PRD addendum: `_bmad-output/planning-artifacts/prd-addenda/epic-16-operator-self-registration.md`
- Epic: `_bmad-output/planning-artifacts/epics/epic-16-operator-self-registration-onboarding.md`
- Stories: `_bmad-output/planning-artifacts/epics/stories/epic-16/`
