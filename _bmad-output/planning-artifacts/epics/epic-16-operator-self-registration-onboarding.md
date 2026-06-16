# Epic 16: Operator Self-Registration & Onboarding

## Goal

Let prospective operators register themselves through the Telegram bot, require admin approval before they enter the Epic 10 operator registry, and guide newly approved operators through Google Calendar connection and linking the **Telegram user account that serves as their customer-facing chat line** (clients DM that account; AI + HITL replies go out on it via per-operator `user_gateway` Telethon sessions).

## Two Telegram Surfaces per Operator

| Surface | Telegram account | Role |
|---------|------------------|------|
| **Platform bot** | **@semantaix_bot** (display name Semantaix) | `/register`, onboarding buttons, admin approval, operator slash commands, HITL operator notifications |
| **Customer line** | Operator's linked user account (`user_gateway`) | Clients message here; inbound → answer pipeline; outbound AI/HITL replies sent from this account |

## In Scope

- Self-service `/register` bot command for non-operators.
- `operator_registration_requests` persistence + admin approve/reject api endpoints.
- Admin approval UX via Telegram inline buttons (`callback_query` — new capability in `bot_gateway`).
- Post-approval onboarding DM with inline buttons reusing Epic 11 calendar connect and Epic 15 QR auth (extended per-operator).
- `operator_onboarding_events` audit trail.
- Per-operator Telethon session files, scoped auth endpoints, **and** per-operator inbound listener + outbound send (customer channel).

## Out of Scope

- Web UI self-registration form.
- Operator self-selection of project (v1 assigns `default` project).
- `/onboarding` re-trigger command (deferred v2).
- Removing admin-created operator paths (`/operator_add`, web CRUD).
- Group/channel customer chat on operator user accounts (private DMs only).
- Unsolicited outbound via operator user accounts.

## Dependencies

- **Epic 10** — `operators` registry, admin username settings, `OperatorRepository`.
- **Epic 11** — `POST /calendar/connect/initiate`, `/connect_calendar` bot command.
- **Epic 15** — `user_gateway` service skeleton + QR auth + message routing patterns (Stories 15.01–15.03 minimum before Epic 16.08).
- **Epic 02** — optional incident on repeated approval failures.

## Delivery Order

```
16-01 (schema + API)
  → 16-02 (/register bot command)
  → 16-03 (callback_query infrastructure)
  → 16-04 (admin approval buttons)
  → 16-05 (onboarding buttons)
  → 16-06 (per-operator QR sessions — Epic 15.02)
  → 16-08 (operator customer channel: inbound + outbound)
  → 16-07 (e2e signoff)
```

**Hard sequencing:** Epic 16 starts after Epic 15 Stories 15.01–15.02. Story 16-08 requires 16-06 + Epic 15.03 routing patterns.

## Exit Criteria

- Non-operator sends `/register` → admin receives approval DM with working buttons.
- Admin approves → operator row exists + applicant receives onboarding DM with two buttons.
- Calendar button → OAuth consent URL DM (same as `/connect_calendar`).
- Telegram button → QR document → linked account active as customer channel.
- Customer DMs operator's **user account** → receives AI answer **from that account**.
- HITL reply → customer receives operator reply **on operator user account**.
- Rejected applicant cannot re-register for 24 h.
- `ruff check .` clean; `pytest --cov` 100% on new modules.
- `scripts/epic16_signoff.sh` green.

## Automated E2E Verification

- Story-aligned tests under `tests/e2e/test_e2e_epic16_*.py` (`@pytest.mark.e2e`, `@pytest.mark.epic("16")`).
- Matrix entry in `_bmad-output/implementation-artifacts/e2e-coverage.md`.

## FR Traceability

| FR | Story |
|----|-------|
| FR-16-01, FR-16-09, FR-16-11 | 16-01 |
| FR-16-01 | 16-02 |
| FR-16-08 | 16-03 |
| FR-16-02, FR-16-03, FR-16-04 | 16-04 |
| FR-16-05, FR-16-06, FR-16-07, FR-16-10 | 16-05 |
| FR-16-07, FR-16-12 | 16-06 |
| FR-16-13, FR-16-14, FR-16-15 | 16-08 |
| All | 16-07 |

Full requirements: `_bmad-output/planning-artifacts/prd-addenda/epic-16-operator-self-registration.md`
