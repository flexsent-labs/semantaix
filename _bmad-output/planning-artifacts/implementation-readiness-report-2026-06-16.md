---
date: 2026-06-16
project: Semantaix
stepsCompleted: [1, 2, 3, 4]
documentsInventoried:
  prd: PRD.md
  architecture: architecture.md
  epics: epics.md
  epicFolder: epics/
  storiesFolder: epics/stories/
  epicsCovered: [1..16]
scope: Epic 16 (Operator Self-Registration & Onboarding) — first IR check
---

## PRD Analysis

Epic 16 requirements are present as a **PRD addendum**:
- `_bmad-output/planning-artifacts/prd-addenda/epic-16-operator-self-registration.md`

As with Epic 15, `PRD.md` itself is not fully backfilled with Epic 16’s FR group. This is acknowledged as a **traceability gap**, not an implementation blocker, because the Epic 16 story set exists under the `epics.md` + `epic-16/` planning folder.

## Epic 16 Scope for This Readiness Check

This is an IR check specifically for kicking off **Story 16.01** (registration-request persistence + API endpoints). The later Epic 16 bot flows (inline callbacks, onboarding DMs, per-operator user_gateway routing, and customer-channel semantics) are intentionally out of scope for this initial “green-light” decision.

## Architecture & Integration Inventory

### Existing patterns we will reuse

- Operator storage pattern exists in `services/api/app/operators.py` (`OperatorRepository`, WAL-free sqlite init, simple CRUD).
- HITL runtime config and authorization patterns already exist in `services/api/app/main.py` and `services/api/app/admin_auth.py` (admin vs internal principal dependencies).

### What Story 16.01 must add

- A new repository module for registration requests + onboarding audit events:
  - Table: `operator_registration_requests` in the operator DB file.
  - Table: `operator_onboarding_events` for audit/metrics.
- API endpoints (internal token + admin session) as described in the planning story:
  - `POST /operators/register-request`
  - `GET /operators/register-requests`
  - `POST /operators/register-requests/{id}/approve`
  - `POST /operators/register-requests/{id}/reject`

## Missing Requirements / Risks

- The Epic 16 stories will introduce new tests and modules; ensure all new code lands under `platform_common/` or `services/` so coverage gates apply.
- The approve path must be atomic (status update + operator insert). This should be implemented using a single sqlite transaction, not two separate repository calls.

## Readiness Decision

Ready to start implementation for:
- **Story 16.01** — `16-01-registration-schema-api` (storage + API contract seam)

Next “must do” actions (BMAD method):
1. Create the implementation story doc in `_bmad-output/implementation-artifacts/`.
2. Flip Story 16.01 to `ready-for-dev` in `_bmad-output/implementation-artifacts/sprint-status.yaml`.

