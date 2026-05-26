# Epic 10.5: Operator-Project Model Refinement

## Goal

Refine the operator-project model shipped in Epic 10 to support the flat-list-multi-operator-per-project pattern that Epic 14 (Usage Monitoring) requires for RBAC scoping. Remove the "primary operator" indirection (`hitl_primary_operator_username` / `hitl_primary_operator_chat_id`), enforce a strict one-operator-per-project constraint at the data layer, and rewrite the bot gateway operator-routing path to resolve operators directly from the `operators` table without a primary-operator fallback.

## In Scope

- Remove `hitl_primary_operator_username` and `hitl_primary_operator_chat_id` from `platform_common/settings.py` and from the `hitl_runtime_config` runtime-overrides table.
- Migration script that backfills the existing primary operator into the `operators` table (if not already present) and removes the two runtime-config rows.
- DB-level constraint: an operator belongs to exactly one project (composite uniqueness on `operators.telegram_username` enforced at write time; existing rows verified during migration).
- Rewrite `services/bot_gateway/app/operator_resolver.py` to resolve operators flatly from the `operators` table — no primary-operator fallback path. API unreachable → fail closed, log `operator_resolution_unavailable`.
- Update `services/bot_gateway/app/main.py` (the `/hitl_config` command, operator-message routing, command dispatch) to use the flat resolution.
- Update `services/api/app/admin_auth.py` (Telegram-code login) to look up admin chat_id via the `operators` table, not the primary-operator settings.
- Update `services/api/app/operator_chat_lookup.py` to resolve any operator's chat_id from the `operators` table, dropping the primary-only guard.
- Update `services/bot_gateway/app/calendar_commands.py` (Epic 11) to pass through to the new resolver without the primary-operator parameter.
- Update the `/hitl_config` command shape: existing form `/hitl_config @user chat_id` becomes `/hitl_config @user chat_id project_slug` (admin-gated; project_slug defaults to the default project). Tests and admin docs updated accordingly.
- Update `epics/README.md` to list Epic 10.5 between Epic 10 and Epic 11.
- Update `e2e-coverage.md` with story-aligned rows.

## Out of Scope

- New multi-operator-routing strategies inside a single project (round-robin, least-loaded, etc.). Epic 10.5 only removes the primary indirection — operator routing within a project (which-operator-handles-this-ticket) stays as it was in Epic 10 (assignment by HITL ticket binding).
- Per-project operator capability flags (read-only, can-edit-KB, etc.) — RBAC remains binary admin/operator (PRD non-goal).
- Cross-project operator transfer (admin moving an operator from project A to project B) via UI — manual DB edit or new ticket-cycle in a later epic.
- Removing the `admin_telegram_username` setting — admin identity stays env-configured (it's not an operator-table identity).
- Backfilling historical HITL tickets with project_id (already done in Epic 10).

## Dependencies

- **Epic 10** — `operators` and `projects` tables, admin-surface scaffolding, RAG project-scoping. All required.
- **Epic 11** — calendar OAuth and `/connect_calendar` command (uses the same operator-resolution path). The Epic 10.5 resolver rewrite must keep `/connect_calendar` working unchanged from the operator's perspective.
- **Epic 14 — Usage Monitoring (planned)** — consumes Epic 10.5's flat operator-project model for `/usage` RBAC scoping. Epic 14 is *the requester*; Epic 10.5 is a blocker for Epic 14 implementation.

## Exit Criteria

- A fresh `docker compose up` boots with no `hitl_primary_operator_username` env or settings reference; the operator (previously primary) is auto-registered into the `operators` table during default-project bootstrap.
- The existing operator continues to receive HITL escalations and KB-upload acks identically — no behavioral change from their perspective.
- Adding a second operator via `/operator_add` (Epic 10) and binding a HITL ticket to that operator yields the same delivery behavior as Epic 10 plus: the second operator's `/files`, `/kb_add`, `/connect_calendar`, and `/hitl_config` commands all route through the flat resolver.
- A second operator added to a *different* project receives only that project's HITL traffic; cross-project leakage asserted absent by E2E.
- `ruff check .` passes; `pytest --cov` shows 100% line coverage on changed modules.
- `scripts/epic10_5_signoff.sh` exits 0 (CI parity + live multi-operator demo).

## Stories

- **10.5-01** — Settings + `hitl_runtime_config` cleanup: remove the two primary-operator keys; one-shot migration backfills existing primary operator into `operators` if absent and deletes runtime-config rows. Blocks all other stories.
- **10.5-02** — Bot gateway resolver rewrite: `operator_resolver.py` resolves flatly from `operators` table; no fallback. Updates to `main.py` and `calendar_commands.py` to pass through. Depends on 10.5-01.
- **10.5-03** — API surface updates: `admin_auth.py`, `operator_chat_lookup.py`, `services/api/app/main.py` use the flat resolver. `/hitl_config` command shape updated. Depends on 10.5-01.
- **10.5-04** — Epic signoff: E2E coverage for the multi-operator-routing happy path + cross-project isolation; `scripts/epic10_5_signoff.sh`; e2e-coverage matrix updated. Depends on 10.5-02 and 10.5-03.

## Automated E2E verification

- Story-aligned tests under `tests/e2e/test_e2e_epic10_5_*.py` (`@pytest.mark.e2e`, `@pytest.mark.epic("10.5")`).
- New scripted signoff: `scripts/epic10_5_signoff.sh`.
- Matrix updated in `_bmad-output/implementation-artifacts/e2e-coverage.md`.
- Required E2Es:
  - **10.5-01** — migration smoke: env without primary keys boots clean; `operators` row exists; `hitl_runtime_config` has no primary-key rows.
  - **10.5-02** — second operator added via `/operator_add` receives HITL escalations identically to the bootstrapped operator; `/files`, `/kb_add` work for both.
  - **10.5-03** — admin login still works (admin chat_id resolved via `operators` table); `/hitl_config @user chat_id project_slug` updates the operator row.
  - **10.5-04** — cross-project isolation: operator A in project P1 never receives a HITL escalation for project P2 (asserted via inbound + assignment + delivery trace).

## Implementation Notes

- This is a **brownfield refinement**, not a feature epic. No new tables; only a column added (`operators.is_admin` or similar — TBD in 10.5-01 if needed) and two runtime-config rows removed.
- Feature-sequential rule (epics/README.md) — Epic 10.5 must complete before Epic 14 starts implementation. Epic 12 (Sales Persona) planning merged via PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) with the materials-cap follow-up in PR [#83](https://github.com/flexsent-labs/semantaix/pull/83); its stories are still `backlog` (in-progress at the epic level per feature-sequential convention). Epic 13 (Unified Project Services Catalog) shipped separately via PR [#80](https://github.com/flexsent-labs/semantaix/pull/80). Epic 10.5 queues behind Epic 12 implementation in `sprint-status.yaml`.
- The migration is one-shot, idempotent. Runs at api startup (matches the Epic 10 bootstrap pattern). Logs `epic_10_5_migration_complete` exactly once per fresh deploy; subsequent boots are no-ops.

## Origin

This epic was created via `bmad-correct-course` on 2026-05-26 in response to design decisions surfaced during the `bmad-brainstorming` session for Epic 14 (Usage Monitoring). See [`brainstorming-session-2026-05-26-1500.md`](../brainstorming/brainstorming-session-2026-05-26-1500.md) — decisions G3 (Phase 1) and the cross-cutting consequences section.
