# Story 14.01 — `semantaix_usage.db` schema, idempotent migration, repository skeletons

## Objective
Lay the data foundation for Epic 14: create the new SQLite store `.data/semantaix_usage.db` (WAL mode) with all five tables (`usage_llm_calls`, `usage_messages`, `usage_hitl_events`, `usage_daily_summary`, `usage_incidents`), pin the indexes, and ship the empty (no-business-logic) repository skeletons that later stories consume. This story ships no customer-visible behavior — it is the substrate every later Epic 14 story builds on.

**As a** platform engineer,
**I want** a dedicated, well-indexed, WAL-mode SQLite store for usage telemetry separated from the rest of the persistence layer,
**So that** scattered instrumentation in later stories can write fire-and-forget without contention against business-critical DBs, and the dashboard / alerts / `/usage` command can read aggregated data without throttling user-facing flows.

PRD reference: **FR-26** (Three-Tracker Usage Architecture), **NFR-10** (Usage Storage Scale), PRD §6 `semantaix_usage.db` row.

## Scope

### In Scope
- **New SQLite store `.data/semantaix_usage.db`** owned by `api` (WAL mode pragma applied at first connection); RO from `web_ui`; RW from `scheduler`.
- **Idempotent migration + fresh-deploy path** in a new `services/api/app/usage/migrations.py` (or reuse `platform_common` migration helper if it exists):
  - For each of the five tables, `CREATE TABLE IF NOT EXISTS` with the final schema.
  - For each new column added in a future story (none in this story), `PRAGMA table_info(<table>)` → `ADD COLUMN` only when absent.
  - For each index, `CREATE INDEX IF NOT EXISTS`.
  - `PRAGMA journal_mode = WAL` applied once per opened connection (sync-safe and idempotent).
- **Final schemas** (frozen as part of this story; later stories may only ADD columns via guarded ALTERs):
  - `usage_llm_calls`: `id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, model_name TEXT NOT NULL, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL, cost_usd REAL, call_outcome TEXT NOT NULL, trace_id TEXT, created_at TEXT NOT NULL`. Index `(project_id, created_at)`.
  - `usage_messages`: `id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, direction TEXT NOT NULL CHECK(direction IN ('in','out')), participant_role TEXT NOT NULL CHECK(participant_role IN ('customer','operator')), trace_id TEXT, created_at TEXT NOT NULL`. Index `(project_id, created_at)`.
  - `usage_hitl_events`: `id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, event_type TEXT NOT NULL CHECK(event_type IN ('created','assigned','replied','resolved')), ticket_id INTEGER NOT NULL, trace_id TEXT, created_at TEXT NOT NULL`. Index `(project_id, created_at)`.
  - `usage_daily_summary`: `project_id INTEGER NOT NULL, day_utc TEXT NOT NULL, tracker_type TEXT NOT NULL CHECK(tracker_type IN ('llm','messages','hitl')), model_name TEXT, prompt_tokens_total INTEGER, completion_tokens_total INTEGER, cost_usd_total REAL, wasted_cost_usd REAL, call_count INTEGER, in_count INTEGER, out_count INTEGER, hitl_created_count INTEGER, hitl_assigned_count INTEGER, hitl_replied_count INTEGER, hitl_resolved_count INTEGER, PRIMARY KEY (project_id, day_utc, tracker_type, model_name)`. Index `(project_id, day_utc, model_name)`. (NULL `model_name` allowed; PK treats NULL distinctly per SQLite semantics — non-LLM rows use `model_name = ''` empty-string sentinel to keep PK behavior unambiguous.)
  - `usage_incidents`: `id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, breached_trackers TEXT NOT NULL, peak_pct REAL, total_excess_cost_usd REAL`. Index `(project_id, started_at)`.
- **New `services/api/app/usage/` package** with empty repository skeletons (sync `sqlite3`; constructor `__init__(self, *, db_path: str)`; methods raise `NotImplementedError` and are implemented in later stories):
  - `UsageLlmCallRepository` — `record(...)`, `list_for_day(...)`.
  - `UsageMessageRepository` — `record(...)`, `count_for_day(...)`.
  - `UsageHitlEventRepository` — `record(...)`, `count_for_day(...)`.
  - `UsageDailySummaryRepository` — `upsert(...)`, `query(...)`, `query_wasted(...)`.
  - `UsageIncidentRepository` — `start(...)`, `expand(...)`, `end(...)`, `active_for_project(...)`, `list_for_window(...)`.
- **Bootstrap call** in the api startup hook (`platform_common/app_factory` or `services/api/app/main.py`): invoke `bootstrap_usage_db(db_path)` once on startup. Idempotent — safe to re-run on every container boot.
- **New `Settings` field** `usage_db_path: str = ".data/semantaix_usage.db"` in `platform_common/settings.py`; add `USAGE_DB_PATH` to `.env.example`.
- **`day_utc` format**: ISO date `YYYY-MM-DD` (no time). All `created_at` columns store full UTC ISO-8601 timestamps with `Z` suffix.

### Out of Scope
- All instrumentation call sites (14.02, 14.03, 14.04 own them).
- `UsageRecorder` ingestion seam + async fire-and-forget queue (14.02 owns it; this story ships only the repos it dispatches to).
- Daily roll-up job + retention purge (14.05).
- Dashboard, API endpoints, bot command, alerting (14.06–14.09).
- Backup-runbook update (14.10).
- Any change to existing `semantaix_*.db` files.

## Implementation Notes
- **Sync `sqlite3` repos** (per project-context rule: SQLite is sync; callers dispatch via `asyncio.to_thread`). No `async def` wrapping `sqlite3`.
- **WAL mode** — applied as `PRAGMA journal_mode = WAL;` at connection open in the bootstrap; safe to re-execute. Verified by reading back `PRAGMA journal_mode` in a test.
- **Migration idempotency** — every `CREATE TABLE` / `CREATE INDEX` uses `IF NOT EXISTS`. The bootstrap is a single function `bootstrap_usage_db(db_path: str) -> None` that opens a connection, applies the WAL pragma, runs all five `CREATE TABLE` + index statements, and closes. Running it twice is byte-identical to running it once.
- **`call_outcome` enum** is enforced at the **Python boundary** (`UsageLlmCallRepository.record` validates against a `frozenset` of allowed values and raises `ValueError` on mismatch). A SQLite `CHECK` constraint is intentionally NOT added — future stories may add new enum values via a code change without a schema migration. (This mirrors how `incident_events.event_type` is enforced today.)
- **`tracker_type` and `event_type` CHECK constraints** — added at the SQL level because they are stable and small (`('llm','messages','hitl')` for tracker_type; `('created','assigned','replied','resolved')` for HITL event_type; `('in','out')` for direction; `('customer','operator')` for participant_role). These are unlikely to change and provide a defense-in-depth guarantee.
- **Empty-string sentinel for non-LLM `model_name`** — `usage_daily_summary` PK includes `model_name`; SQLite treats NULL as non-equal in UNIQUE/PK comparisons, which would cause duplicate rows for the messages/HITL trackers. The roll-up worker (14.05) writes `model_name = ''` for non-LLM rows; this story documents the convention in a module-level docstring.
- **Frozen dataclasses** — `UsageLlmCallRow`, `UsageMessageRow`, `UsageHitlEventRow`, `UsageDailySummaryRow`, `UsageIncidentRow` as `@dataclass(frozen=True)`. Constructed by the repos in later stories; this story defines the shapes.
- **`Settings` integration** — `usage_db_path` follows the existing pattern (cf. `hitl_db_path` if it exists, or the catalog DB pattern). Bootstrap reads from `Settings.usage_db_path` at startup time.
- **Structured logging** — bootstrap emits `usage_db_bootstrapped` on success (with `tables_created` count: `5` on first run, `0` on subsequent runs since `IF NOT EXISTS`). Verified by capturing log output in the bootstrap test.
- **No `secrets` / `hmac` / `Fernet`** — no auth surface in this story. Money RBAC + auth land in 14.07.

## Test Plan

### Unit
- `tests/test_usage_db_migrations.py`:
  - **Idempotency:** create a fresh DB, run `bootstrap_usage_db` twice → second run is a no-op (same `sqlite_master` row count, same `PRAGMA table_info` for every table, same `PRAGMA index_list`).
  - **WAL mode:** after bootstrap, `PRAGMA journal_mode` returns `wal`.
  - **All five tables exist** with the documented columns + CHECK constraints. Use `PRAGMA table_info` to assert column name + type + notnull. Use `sqlite_master.sql` to assert CHECK constraint substrings.
  - **All required indexes exist** — `PRAGMA index_list` confirms `usage_llm_calls_project_created_idx`, `usage_messages_project_created_idx`, `usage_hitl_events_project_created_idx`, `usage_daily_summary_project_day_model_idx`, `usage_incidents_project_started_idx` (or the names the implementation chooses — document them in the migration module).
  - **CHECK constraint enforcement:** inserting `direction = 'sideways'` into `usage_messages` raises `sqlite3.IntegrityError`; inserting `event_type = 'bogus'` into `usage_hitl_events` raises; inserting `tracker_type = 'bogus'` into `usage_daily_summary` raises.
- `tests/test_usage_repositories_skeleton.py`:
  - Constructing each repo with `db_path=":memory:"` after bootstrap succeeds.
  - Calling each not-yet-implemented method raises `NotImplementedError` (and the test documents which story implements each).

### Contract
- N/A — no api endpoints in this story.

### Integration
- `tests/test_api_startup_bootstrap_epic14.py` — boot the api with a fresh `.data/`; assert `semantaix_usage.db` exists, is WAL mode, and contains all five tables with their indexes. Stop + restart the api → no errors in startup logs.

## Automated E2E verification
None for this story (no externally observable behavior). Coverage is unit + integration only.

## Manual Verification
1. `docker compose up --build api` against a fresh `.data/` → confirm `.data/semantaix_usage.db` exists.
2. `sqlite3 .data/semantaix_usage.db "PRAGMA journal_mode;"` → returns `wal`.
3. `sqlite3 .data/semantaix_usage.db ".tables"` → lists `usage_llm_calls`, `usage_messages`, `usage_hitl_events`, `usage_daily_summary`, `usage_incidents`.
4. `sqlite3 .data/semantaix_usage.db ".schema usage_llm_calls"` shows the documented columns including the `call_outcome` text column without a CHECK (enforced in Python).
5. Stop + restart the api container → confirm `usage_db_bootstrapped` log appears on each boot; no `duplicate column name` / `table already exists` errors.

## Done Criteria
- 100% line coverage on `services/api/app/usage/migrations.py` + repository skeletons + new `Settings` field path.
- `ruff check .` passes.
- Bootstrap is idempotent (verified by run-twice test).
- WAL mode verified active.
- All five tables + indexes present with correct shapes.
- `usage_db_bootstrapped` structured-log event emitted on startup.
- `USAGE_DB_PATH` documented in `.env.example`.
- Other tables in `.data/*.db` are untouched by this migration (snapshot test verifying no `semantaix_*.db` file other than the new one is opened by the bootstrap).
