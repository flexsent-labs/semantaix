# Story 14.01: `semantaix_usage.db` schema, idempotent migration, repository skeletons

Status: ready-for-dev

## Story

As a platform engineer,
I want a dedicated, well-indexed, WAL-mode SQLite store for usage telemetry,
so that scattered instrumentation in later stories can write fire-and-forget without contention
against business-critical DBs, and the dashboard / alerts / `/usage` command can read aggregated
data without throttling user-facing flows.

## Acceptance Criteria

1. `services/api/app/usage/migrations.py` exposes `bootstrap_usage_db(db_path: str) -> None` that
   creates all five tables + indexes + WAL pragma. Calling it twice on the same file is a no-op
   (same `sqlite_master` count, same `PRAGMA table_info`, same `PRAGMA index_list`).
2. All five tables exist with the exact column schemas and CHECK constraints documented below.
3. All five indexes exist with the exact names documented below.
4. `PRAGMA journal_mode` returns `wal` after bootstrap.
5. `bootstrap_usage_db` emits a structured log `usage_db_bootstrapped` on success (carrying
   `tables_created=5` on first run, `0` on idempotent re-run).
6. Five frozen-dataclass row types (`UsageLlmCallRow`, `UsageMessageRow`, `UsageHitlEventRow`,
   `UsageDailySummaryRow`, `UsageIncidentRow`) are defined in `services/api/app/usage/repositories.py`.
7. Five repository skeletons with `__init__(self, *, db_path: str)` and all methods raising
   `NotImplementedError` are defined in the same file.
8. `platform_common/settings.py` has `usage_db_path: str = ".data/semantaix_usage.db"`.
9. `USAGE_DB_PATH=.data/semantaix_usage.db` is added to `.env.example`.
10. `services/api/app/main.py` calls `bootstrap_usage_db(settings.usage_db_path)` at module-scope
    startup (after the sales bootstrap, before the first route is registered).
11. `ruff check .` is clean; `pytest --cov` reaches 100% line coverage on the new modules.

## Tasks / Subtasks

- [ ] Task 1 — Create `services/api/app/usage/` package (AC: 1, 2, 3, 4, 5)
  - [ ] 1.1 Add `services/api/app/usage/__init__.py` (empty)
  - [ ] 1.2 Create `services/api/app/usage/migrations.py` with `bootstrap_usage_db`
  - [ ] 1.3 Apply WAL pragma before DDL statements
  - [ ] 1.4 Emit `usage_db_bootstrapped` structured log with `tables_created` count

- [ ] Task 2 — Define frozen dataclasses + repository skeletons (AC: 6, 7)
  - [ ] 2.1 Create `services/api/app/usage/repositories.py` with five `@dataclass(frozen=True)` row types
  - [ ] 2.2 Implement five repository classes; every method body is `raise NotImplementedError`
  - [ ] 2.3 Each `NotImplementedError` docstring names which story implements the method

- [ ] Task 3 — Settings + `.env.example` (AC: 8, 9)
  - [ ] 3.1 Add `usage_db_path: str = ".data/semantaix_usage.db"` to `platform_common/settings.py`
  - [ ] 3.2 Add `USAGE_DB_PATH=.data/semantaix_usage.db` to `.env.example`

- [ ] Task 4 — Wire bootstrap into api startup (AC: 10)
  - [ ] 4.1 Import `bootstrap_usage_db` from `services.api.app.usage.migrations` in `main.py`
  - [ ] 4.2 Call `bootstrap_usage_db(settings.usage_db_path)` after `sales_bootstrap_init_schema` call (line ~506)

- [ ] Task 5 — Tests (AC: 1–5, 7, 10, 11)
  - [ ] 5.1 Create `tests/test_usage_db_migrations.py` (idempotency, WAL, tables, columns, indexes, CHECK violations)
  - [ ] 5.2 Create `tests/test_usage_repositories_skeleton.py` (construction succeeds; each method raises `NotImplementedError`)
  - [ ] 5.3 Create `tests/test_api_startup_bootstrap_epic14.py` (startup test: db exists, WAL, 5 tables + indexes)

## Dev Notes

### Package layout (all new files)

```
services/api/app/usage/
    __init__.py               — empty
    migrations.py             — bootstrap_usage_db + _connect helper
    repositories.py           — 5 row dataclasses + 5 repository skeletons
```

### `migrations.py` — implementation pattern

Mirror `services/api/app/web_auth.py` exactly for the WAL + CREATE TABLE IF NOT EXISTS pattern:

```python
from __future__ import annotations
import logging
import sqlite3
from pathlib import Path

_LOG = logging.getLogger(__name__)

def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def bootstrap_usage_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        before = _table_count(conn)
        # ... five CREATE TABLE IF NOT EXISTS + five CREATE INDEX IF NOT EXISTS ...
        after = _table_count(conn)
    _LOG.info("usage_db_bootstrapped", extra={"tables_created": after - before})

def _table_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
```

**Important:** the `tables_created` counter must reflect the number of *newly* created tables, not
always 5. On first run it's 5; on re-run it's 0. Use `_table_count` before and after to compute the
delta. Alternatively, count tables before and after and subtract.

**Structured log convention** (mirror existing usage across the codebase):
```python
_LOG.info("usage_db_bootstrapped", extra={"tables_created": after - before})
```
Matches the pattern used in `services/api/app/sales/bootstrap.py` and other modules.

### Five table schemas (exact DDL to implement)

```sql
CREATE TABLE IF NOT EXISTS usage_llm_calls (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    cost_usd REAL,
    call_outcome TEXT NOT NULL,
    trace_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_llm_calls_project_created_idx
    ON usage_llm_calls (project_id, created_at);

CREATE TABLE IF NOT EXISTS usage_messages (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
    participant_role TEXT NOT NULL CHECK(participant_role IN ('customer','operator')),
    trace_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_messages_project_created_idx
    ON usage_messages (project_id, created_at);

CREATE TABLE IF NOT EXISTS usage_hitl_events (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('created','assigned','replied','resolved')),
    ticket_id INTEGER NOT NULL,
    trace_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_hitl_events_project_created_idx
    ON usage_hitl_events (project_id, created_at);

CREATE TABLE IF NOT EXISTS usage_daily_summary (
    project_id INTEGER NOT NULL,
    day_utc TEXT NOT NULL,
    tracker_type TEXT NOT NULL CHECK(tracker_type IN ('llm','messages','hitl')),
    model_name TEXT NOT NULL DEFAULT '',
    prompt_tokens_total INTEGER,
    completion_tokens_total INTEGER,
    cost_usd_total REAL,
    wasted_cost_usd REAL,
    call_count INTEGER,
    in_count INTEGER,
    out_count INTEGER,
    hitl_created_count INTEGER,
    hitl_assigned_count INTEGER,
    hitl_replied_count INTEGER,
    hitl_resolved_count INTEGER,
    PRIMARY KEY (project_id, day_utc, tracker_type, model_name)
);
CREATE INDEX IF NOT EXISTS usage_daily_summary_project_day_model_idx
    ON usage_daily_summary (project_id, day_utc, model_name);

CREATE TABLE IF NOT EXISTS usage_incidents (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    breached_trackers TEXT NOT NULL,
    peak_pct REAL,
    total_excess_cost_usd REAL
);
CREATE INDEX IF NOT EXISTS usage_incidents_project_started_idx
    ON usage_incidents (project_id, started_at);
```

**Critical design note — `call_outcome` has NO CHECK constraint.** This is intentional: future
stories add new outcome values (e.g. `moderation_triggered`) via code changes only, without a schema
migration. Validation is enforced at the Python boundary in `UsageLlmCallRepository.record` using a
`frozenset`. This mirrors `incidents.py`'s `event_type` enforcement pattern.

**`model_name` sentinel:** `usage_daily_summary.model_name` uses `DEFAULT ''` (empty string), not
NULL. SQLite treats NULL as non-equal in UNIQUE/PK comparisons, so non-LLM rows (messages / HITL
trackers) use `model_name = ''` to keep PK deterministic. Document this in a module-level docstring.

### Repository pattern for skeletons

Mirror `services/api/app/sales/state_repository.py` for the class structure but every method body
must be `raise NotImplementedError`. Example:

```python
@dataclass(frozen=True)
class UsageLlmCallRow:
    id: int
    project_id: int
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    call_outcome: str
    trace_id: str | None
    created_at: str  # UTC ISO-8601 with Z suffix

class UsageLlmCallRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def record(self, row: UsageLlmCallRow) -> None:
        """Implemented in Story 14.02."""
        raise NotImplementedError

    def list_for_day(self, *, project_id: int, day_utc: str) -> list[UsageLlmCallRow]:
        """Implemented in Story 14.05 (roll-up reads raw rows)."""
        raise NotImplementedError
```

Repeat for all five repos. The `NotImplementedError` docstring naming the implementing story is
required — it prevents future devs from accidentally implementing the wrong story's scope.

### Settings field placement

In `platform_common/settings.py`, add after `calendar_db_path` (line ~169):
```python
usage_db_path: str = ".data/semantaix_usage.db"
```

### main.py bootstrap call

In `services/api/app/main.py`, after the sales bootstrap call (~line 506):
```python
from services.api.app.usage.migrations import bootstrap_usage_db
# ... (existing imports at top) ...
bootstrap_usage_db(settings.usage_db_path)
```

The import goes at the top of the file with other local imports. The call goes at module scope,
after `sales_bootstrap_init_schema(settings.sales_db_path)` and before repository construction.

### Project Structure Notes

- New package `services/api/app/usage/` mirrors `services/api/app/sales/` in structure
- No sub-packages needed for this story; all in one module level
- `__init__.py` is empty — no public API re-exports at this story
- `repositories.py` will gain real implementations in 14.02–14.05; the file grows incrementally
- Index naming convention: `{table_name}_{qualifier}_idx` (matches existing pattern, e.g.
  `idx_web_auth_codes_username_active` from `web_auth.py` — note existing code uses `idx_` prefix;
  the story spec uses suffix `_idx`. **Use `_idx` suffix as specified in the story planning doc.**

### Test patterns

**`tests/test_usage_db_migrations.py`** — call `bootstrap_usage_db(str(tmp_path / "usage.db"))`:
```python
def test_idempotency(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        before = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        before_idx = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    bootstrap_usage_db(db)  # second run
    with sqlite3.connect(db) as conn:
        after = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        after_idx = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
    assert before == after
    assert before_idx == after_idx

def test_wal_mode(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"

def test_check_violation_direction(tmp_path):
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    with sqlite3.connect(db) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO usage_messages (project_id, direction, participant_role, created_at) "
            "VALUES (1, 'sideways', 'customer', '2026-06-11T00:00:00Z')"
        )
```

**`tests/test_usage_repositories_skeleton.py`** — construct each repo with `":memory:"` and call
`bootstrap_usage_db` first (`:memory:` is valid for testing); assert each method raises
`NotImplementedError`.

**`tests/test_api_startup_bootstrap_epic14.py`** — mirror `tests/test_api_startup_bootstrap.py`.
Patch `api_main.settings.usage_db_path` to `str(tmp_path / "usage.db")` via monkeypatch, then call
`api_main.bootstrap_usage_db(api_main.settings.usage_db_path)` directly (do NOT re-import main;
that would re-run module-level code). Assert the DB exists, `PRAGMA journal_mode = wal`, and all
five tables are present.

### References

- WAL + CREATE TABLE IF NOT EXISTS pattern: [Source: services/api/app/web_auth.py#init_schema]
- Bootstrap split pattern: [Source: services/api/app/sales/bootstrap.py]
- Repository skeleton pattern: [Source: services/api/app/sales/state_repository.py]
- Settings db_path field pattern: [Source: platform_common/settings.py]
- main.py bootstrap call placement: [Source: services/api/app/main.py:506]
- .env.example db_path pattern: [Source: .env.example]
- Story schemas and index names: [Source: _bmad-output/planning-artifacts/epics/stories/epic-14/story-14-01-usage-db-schema-and-migration.md]
- Epic 14 table spec: [Source: _bmad-output/planning-artifacts/epics/epic-14-usage-cost-monitoring.md]

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

### Completion Notes List

### File List

- `services/api/app/usage/__init__.py` — NEW
- `services/api/app/usage/migrations.py` — NEW
- `services/api/app/usage/repositories.py` — NEW
- `platform_common/settings.py` — UPDATE (add `usage_db_path`)
- `services/api/app/main.py` — UPDATE (import + call `bootstrap_usage_db`)
- `.env.example` — UPDATE (add `USAGE_DB_PATH`)
- `tests/test_usage_db_migrations.py` — NEW
- `tests/test_usage_repositories_skeleton.py` — NEW
- `tests/test_api_startup_bootstrap_epic14.py` — NEW
