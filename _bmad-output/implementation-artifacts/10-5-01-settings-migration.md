# Story 10.5-01: Settings + hitl_runtime_config cleanup migration

## Status: in-progress

## Story
As a developer, I want the `hitl_primary_operator_username` and `hitl_primary_operator_chat_id`
settings-and-runtime-config indirection removed so that the flat operators-table model is the
single source of truth for operator identity.

## Acceptance Criteria
1. `_bootstrap_default_entities()` in `services/api/app/main.py` deletes
   `hitl_primary_operator_username` and `hitl_primary_operator_chat_id` rows from
   `hitl_runtime_config` after ensuring the default operator exists in the `operators` table.
2. The migration is idempotent — re-running it on a DB where the rows are already absent is
   a no-op.
3. The default operator row in `operators` is still correctly seeded from
   `settings.hitl_primary_operator_username` / `settings.hitl_primary_operator_chat_id`
   (settings fields kept for this story — removed in 10.5-03).
4. `ruff check .` passes; `pytest --cov` shows 100% on changed modules.

## Tasks/Subtasks
- [x] 1. Add migration calls in `_bootstrap_default_entities()` in `services/api/app/main.py`
  - [x] 1.1 After `ensure_default_operator()`, delete `hitl_primary_operator_username` from `hitl_runtime_config`
  - [x] 1.2 After `ensure_default_operator()`, delete `hitl_primary_operator_chat_id` from `hitl_runtime_config`
- [x] 2. Add `delete_runtime_config` method to `HitlTicketRepository` (needed for the migration)
- [x] 3. Tests: `tests/test_story_10_5_01_migration.py`
  - [x] 3.1 Migration deletes both runtime-config rows (pre-existing rows absent after bootstrap)
  - [x] 3.2 Migration is idempotent (running twice does not error)
  - [x] 3.3 Default operator still seeded in operators table after migration

## Dev Notes
- `HitlTicketRepository` lives in `services/api/app/hitl.py` — check if it already has a delete method.
- The `set_runtime_config` upsert is at `hitl.py:HitlTicketRepository.set_runtime_config`.
- `_bootstrap_default_entities()` is at `services/api/app/main.py` ~line 423.
- Keep `settings.hitl_primary_operator_username` and `settings.hitl_primary_operator_chat_id` in
  `platform_common/settings.py` — they are still used as the migration seed value.
  These are removed in Story 10.5-03.

## Dev Agent Record

### Completion Notes
- Added `delete_runtime_config(key)` to `HitlTicketRepository` in `services/api/app/hitl.py`.
- Extended `_bootstrap_default_entities()` to delete both primary-operator runtime-config rows
  after seeding the operators table.
- Idempotent: `DELETE WHERE key = ?` is a no-op when the row is absent.
- Tests cover: pre-populated rows deleted, idempotent re-run, operators table still seeded.

## File List
- services/api/app/hitl.py
- services/api/app/main.py
- tests/test_story_10_5_01_migration.py

## Change Log
- 2026-06-14: Story 10.5-01 implemented (settings migration + hitl_runtime_config cleanup)
