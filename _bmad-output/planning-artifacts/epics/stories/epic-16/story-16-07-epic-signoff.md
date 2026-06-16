# Story 16.07 — Epic 16 Signoff

## Objective

Prove the full operator self-registration and onboarding journey end-to-end: register → admin approve → onboarding buttons → calendar URL → Telegram QR → **customer DM on operator account → AI reply from operator account**.

**As a** platform operator,
**I want** Epic 16 covered by automated signoff,
**So that** registration and onboarding regressions are caught in CI.

## Scope

### In Scope

- **`scripts/epic16_signoff.sh`**:
  - `ruff check .`
  - `pytest --cov --cov-config=.coveragerc --cov-report=term-missing` (100% gate)
  - `pytest -m e2e tests/e2e/test_e2e_epic16_*.py -v`

- **E2E tests**:
  - `tests/e2e/test_e2e_epic16_registration_journey.py` — unknown user `/register` → pending request in DB → simulated admin callback approve → operator row → onboarding event `onboarding_sent`.
  - `tests/e2e/test_e2e_epic16_admin_approval.py` — callback approve/reject paths.
  - `tests/e2e/test_e2e_epic16_onboarding_buttons.py` — calendar button → consent URL captured; telegram button → qr_start called (mock user_gateway).
  - `tests/e2e/test_e2e_epic16_operator_customer_channel.py` — customer DM on operator user account → inbound with `delivery_channel=operator_user` → outbound send on same account.

- **Docs**:
  - Update `_bmad-output/implementation-artifacts/e2e-coverage.md` matrix.
  - Update `CLAUDE.md` / `AGENTS.md` bot command table: `/register`, callback buttons, onboarding flow one-liner.

- **Housekeeping**:
  - Append Epic 16 rows to FR coverage map in `epics.md`.
  - Amend `PRD.md` with Epic 16 feature group (or link to prd-addenda).

### Out of Scope

- Production Telethon QR scan (manual only).

## Done Criteria

- `scripts/epic16_signoff.sh` exits 0.
- All FR-16-01 through FR-16-12 have at least one automated test reference in e2e-coverage.md.
