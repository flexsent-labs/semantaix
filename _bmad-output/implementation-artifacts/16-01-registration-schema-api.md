# Story 16.01 — Registration Requests Schema + API

Status: ready-for-dev

<!-- Created via bmad-create-story. -->

## Story

As a **platform engineer**,
I want **operator self-registration requests** to be persisted and to have **admin-gated API endpoints**,
so that the Semantaix operator onboarding bot flow can create, review, approve/reject, and audit applications without relying on ephemeral state.

## Objective

Land the persistence and API seam for operator self-registration:
- pending registration request storage keyed by applicant username + chat_id
- approve/reject mutations
- onboarding event audit trail

## Acceptance Criteria

1. **Repository schema exists.**
   - Table `operator_registration_requests` exists with at least:
     `id, username, chat_id, display_name, status, project_id, created_at, reviewed_at, reviewed_by, rejection_cooldown_until`
   - Table `operator_onboarding_events` exists for audit/metrics.
   - Unique pending-username behavior is enforced via an index/constraint (pending conflict prevents duplicate pending requests).

2. **Repository behavior is correct and idempotent.**
   - `create_request()`:
     - creates a new pending request
     - rejects duplicate pending requests for the same username
     - rejects re-register attempts during cooldown after rejection
   - `approve()`:
     - atomically updates request `status -> approved`
     - creates the operator row (`operators` table) in the same sqlite transaction
     - records an onboarding event (at minimum `approved`)
   - `reject()`:
     - updates request `status -> rejected`
     - sets `rejection_cooldown_until = now + 24h`

3. **Internal API contract exists (Bearer token).**
   - `POST /operators/register-request` returns `{request_id, status: "pending"}`.
   - `GET /operators/register-requests?status=pending` returns only pending requests and is admin-gated.
   - `POST /operators/register-requests/{id}/approve`:
     - returns the created operator payload
     - returns `409` on username conflict with an existing operator
   - `POST /operators/register-requests/{id}/reject` updates the request as rejected.

4. **Validation + errors are stable.**
   - Username normalization strips leading `@` and stores with `@` prefix (matching `OperatorRepository` convention).
   - Cooldown vs pending-conflict failures produce consistent HTTP errors (documented in the API contract tests).

5. **Test plan passes with 100% coverage for new code.**
   - New/updated tests cover:
     - pending-conflict path
     - cooldown path
     - approve atomicity outcomes
     - reject cooldown field behavior
     - API authorization enforcement (internal vs admin)

## Tasks / Subtasks

- [ ] Add `services/api/app/operator_registration.py`:
  - [ ] Implement `OperatorRegistrationRepository` (schema init + sqlite connection handling)
  - [ ] Implement request lifecycle:
    - [ ] `create_request`
    - [ ] `list_by_status`
    - [ ] `get`
    - [ ] `approve` (single-transaction update + operator insert)
    - [ ] `reject` (cooldown set)
    - [ ] `record_onboarding_event`
  - [ ] Define repository exceptions used by API layer

- [ ] Wire API endpoints into `services/api/app/main.py` (or dedicated router, then imported):
  - [ ] `POST /operators/register-request`
  - [ ] `GET /operators/register-requests`
  - [ ] `POST /operators/register-requests/{id}/approve`
  - [ ] `POST /operators/register-requests/{id}/reject`
  - [ ] Use existing `require_admin_or_internal` / admin-session dependencies consistently

- [ ] Add repository + contract tests:
  - [ ] `tests/test_operator_registration_repository.py`
  - [ ] `tests/test_api_operator_registration_contract.py`

- [ ] Lint + formatting:
  - [ ] `ruff check .`

## Dev Notes

### Files to touch

- `services/api/app/operator_registration.py` *(NEW — operator registration persistence)*
- `services/api/app/main.py` *(UPDATE — add the 4 endpoints)*
- `tests/test_operator_registration_repository.py` *(NEW)*
- `tests/test_api_operator_registration_contract.py` *(NEW)*
- `_bmad-output/implementation-artifacts/sprint-status.yaml` *(UPDATE — ready-for-dev gate)*

### Integration expectations

- Store registration requests in the same sqlite file as the operators (`semantaix_operators.db`) so approvals can join to the canonical `operators` table.
- Normalize Telegram usernames exactly once at API boundary.
- Approvals must not create a partial state if operator creation fails (409 conflict should not leave a request stuck in approved).

## Test Plan

1. `pytest -q tests/test_operator_registration_repository.py -v`
2. `pytest -q tests/test_api_operator_registration_contract.py -v`
3. Full suite (CI parity): `pytest --cov --cov-config=.coveragerc --cov-report=term-missing`

## Done Criteria

- Story 16.01 is implemented with:
  - repository correctness
  - endpoint contract correctness
  - 100% coverage on new code paths
- No regressions in existing Epic 14/15 flows.

