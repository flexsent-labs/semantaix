# Story 14.05: Daily roll-up worker + 30-day raw retention purge

Status: done

## Story

As an admin,
I want the dashboard, alerts, and `/usage` command to be fast and accurate by reading pre-aggregated daily summaries instead of scanning 30 days of raw rows for every chart tick,
so that WAL contention stays low and queries respond in milliseconds even as the project scales toward 100k LLM calls/day.

## Acceptance Criteria

1. `UsageDailySummaryRepository.upsert(row: UsageDailySummaryRow) -> None` is implemented in `services/api/app/usage/repositories.py`:
   - `INSERT … ON CONFLICT(project_id, day_utc, tracker_type, model_name) DO UPDATE SET …` (all counter columns)
   - Idempotent: calling with the same PK twice produces exactly one row

2. `UsageDailySummaryRepository.query(*, project_id, from_day_utc, to_day_utc, trackers: list[str] | None = None) -> list[UsageDailySummaryRow]` is implemented:
   - Returns rows ordered by `(day_utc, tracker_type, model_name)`
   - When `trackers` is non-empty, filters to only those tracker types
   - When `trackers` is `None` or empty, returns all trackers

3. `purge_before(cutoff_iso: str, batch_size: int = 10_000) -> int` is added to `UsageLlmCallRepository`, `UsageMessageRepository`, `UsageHitlEventRepository`:
   - Deletes rows where `created_at < cutoff_iso` in batches of `batch_size` using `LIMIT`
   - Returns total `rows_deleted` across all batches
   - Commits between batches (releases WAL lock between rounds)

4. Three new Settings fields added to `platform_common/settings.py` and `.env.example`:
   - `usage_daily_rollup_hour_utc: int = 0`
   - `usage_raw_retention_days: int = 30`
   - `usage_rollup_batch_size: int = 10_000`

5. `services/scheduler/app/usage_rollup.py` implements `async def run_rollup(*, clock, repos) -> None`:
   - Discovers active project_ids via UNION query on all three raw tables
   - For each project_id, for each day between (last-summary-day + 1) and yesterday (up to 30-day cap): aggregates all three tracker types and calls `repos.summary.upsert()`
   - LLM aggregation: one row per `(project, day, 'llm', model_name)` with token/cost/wasted_cost totals + call_count
   - Messages aggregation: one row per `(project, day, 'messages', '')` with in_count + out_count + call_count
   - HITL aggregation: one row per `(project, day, 'hitl', '')` with all four event type counts
   - Empty days produce no summary rows
   - Structured logs: `usage_rollup_started`, `usage_rollup_day_completed`, `usage_rollup_completed`

6. `services/scheduler/app/usage_retention.py` implements `async def run_retention(*, clock, repos, retention_days: int = 30) -> None`:
   - Purges rows from all three raw tables where `created_at < cutoff`
   - Cutoff: `clock() - timedelta(days=retention_days)`, truncated to ISO-8601 seconds (`...Z`)
   - Runs purge on each table in order; does NOT touch `usage_daily_summary`
   - Structured logs: `usage_retention_started`, `usage_retention_purged` (per table), `usage_retention_completed`

7. Scheduler service wired: two new `_Job`-compatible classes added to `services/scheduler/app/jobs/` and registered in `_build_jobs()` in `services/scheduler/app/main.py`:
   - `UsageRollupJob` — due check: `clock() > today's rollup time AND last_run_date < today_utc`
   - `UsageRetentionJob` — due check: `clock() > today's retention time AND last_run_date < today_utc`; runs after rollup
   - State persisted in `.data/scheduler_runs.json` (`{last_rollup_date_utc, last_retention_date_utc}`)
   - On first boot (no state file), both jobs run immediately for the current day

8. 100% line coverage on all new code paths. `ruff check .` clean. Idempotency verified by run-twice test. Day-by-day catchup verified. Bounded retention batch verified.

## Tasks / Subtasks

- [x] Task 1 — Repository layer (AC: 1, 2, 3)
  - [x] 1.1 Implement `UsageDailySummaryRepository.upsert(row)` — INSERT … ON CONFLICT DO UPDATE SET (all counter columns)
  - [x] 1.2 Implement `UsageDailySummaryRepository.query(*, project_id, from_day_utc, to_day_utc, trackers)` — ordered SELECT with optional tracker filter
  - [x] 1.3 Add `UsageLlmCallRepository.purge_before(cutoff_iso, batch_size)` — batched DELETE with LIMIT loop
  - [x] 1.4 Add `UsageMessageRepository.purge_before(cutoff_iso, batch_size)` — same pattern
  - [x] 1.5 Add `UsageHitlEventRepository.purge_before(cutoff_iso, batch_size)` — same pattern
  - [x] 1.6 Update `tests/test_usage_repositories_skeleton.py` — change `test_daily_summary_repo_upsert_not_implemented` → `test_daily_summary_repo_upsert_is_implemented`; change `test_daily_summary_repo_query_not_implemented` to test the new signature (not raise)

- [x] Task 2 — Settings + .env.example (AC: 4)
  - [x] 2.1 Add three fields to `AppSettings` in `platform_common/settings.py`
  - [x] 2.2 Add three entries to `.env.example` (under the Epic 14 section)

- [x] Task 3 — Rollup job module (AC: 5)
  - [x] 3.1 Create `services/scheduler/app/usage_rollup.py` with `run_rollup(*, clock, repos)` entry point
  - [x] 3.2 Implement project discovery UNION query
  - [x] 3.3 Implement catchup loop (from last-summary-day+1 to yesterday, capped at 30)
  - [x] 3.4 Implement LLM aggregation SQL (per model_name, wasted_cost via FILTER)
  - [x] 3.5 Implement messages aggregation SQL (in_count, out_count, call_count)
  - [x] 3.6 Implement HITL aggregation SQL (four event_type counts pivoted into one row)
  - [x] 3.7 Structured logging at start, per-day, and completion

- [x] Task 4 — Retention module (AC: 6)
  - [x] 4.1 Create `services/scheduler/app/usage_retention.py` with `run_retention(*, clock, repos, retention_days)` entry point
  - [x] 4.2 Compute cutoff as ISO string from `clock() - timedelta(days=retention_days)`
  - [x] 4.3 Call `purge_before()` on each of the three raw repos; accumulate row counts
  - [x] 4.4 Structured logging

- [x] Task 5 — Scheduler wiring (AC: 7)
  - [x] 5.1 Create `services/scheduler/app/jobs/usage_rollup_job.py` — `UsageRollupJob` implementing `_Job` protocol with due check + `scheduler_runs.json` state
  - [x] 5.2 Create `services/scheduler/app/jobs/usage_retention_job.py` — `UsageRetentionJob` same pattern; runs after rollup
  - [x] 5.3 Register both jobs in `_build_jobs()` in `services/scheduler/app/main.py`

- [x] Task 6 — Tests (AC: 8)
  - [x] 6.1 `tests/test_usage_daily_summary_repository.py` — upsert (insert + update), query (tracker filter, all trackers, ordering)
  - [x] 6.2 `tests/test_usage_rollup_aggregation.py` — full aggregation, multiple models, empty day, idempotent re-run, catchup
  - [x] 6.3 `tests/test_usage_retention.py` — purge old rows, keep recent, batched, summary rows untouched
  - [x] 6.4 `tests/test_scheduler_loop_due_check.py` — due/not-due logic, state persistence, ordering
  - [x] 6.5 `tests/test_scheduler_runs_rollup_and_retention.py` — integration: seed → rollup → assert summaries → retention → assert purge
  - [x] 6.6 `tests/e2e/test_e2e_epic14_rollup_and_retention.py` — full lifecycle, catchup, retention override

## Dev Notes

### Existing code patterns — CRITICAL

**Do NOT bypass `SchedulerRunner`.** The existing `main.py` already has `runner = SchedulerRunner(jobs=_build_jobs(), ...)` and it is well-tested. Add new jobs by extending `_build_jobs()` only. Do NOT replace the loop.

**`_Job` Protocol** (from `services/scheduler/app/runner.py`):
```python
class _Job(Protocol):
    name: str
    async def run(self) -> Any: ...
```
Each tick, `runner.tick_once()` calls `await job.run()` on every registered job. A job that should only run once per day must implement its own due-check inside `run()`. Exceptions are caught by the runner's `except Exception` broad guard — jobs should log their own errors before raising.

**`services/scheduler/app/jobs/proactive_followup.py`** — reference implementation for a job with state (clock injection, `async def run()`, structured logging). Mirror its pattern.

**Clock injection**: Always accept `clock: Callable[[], datetime]` as a constructor or function parameter. `datetime.now(UTC)` should never appear in job/module logic — only in the default argument. This is the 100%-gate requirement from `project-context.md`.

**`sqlite3` is sync, async boundaries use `asyncio.to_thread()`**: All SQLite work in repositories is synchronous. The rollup and retention modules call repos via `await asyncio.to_thread(repo.method, ...)` — never call them directly in an async context.

**Structured logging format** — `snake_case verb_noun` event name, no f-strings in the message key:
```python
logger.info("usage_rollup_started", extra={"from_day_utc": from_day, "project_count": n})
```

### `UsageDailySummaryRepository.upsert` — SQL pattern

```python
def upsert(self, row: UsageDailySummaryRow) -> None:
    with sqlite3.connect(self._db_path) as conn:
        conn.execute(
            """
            INSERT INTO usage_daily_summary
                (project_id, day_utc, tracker_type, model_name,
                 prompt_tokens_total, completion_tokens_total, cost_usd_total,
                 wasted_cost_usd, call_count, in_count, out_count,
                 hitl_created_count, hitl_assigned_count, hitl_replied_count,
                 hitl_resolved_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id, day_utc, tracker_type, model_name)
            DO UPDATE SET
                prompt_tokens_total    = excluded.prompt_tokens_total,
                completion_tokens_total= excluded.completion_tokens_total,
                cost_usd_total         = excluded.cost_usd_total,
                wasted_cost_usd        = excluded.wasted_cost_usd,
                call_count             = excluded.call_count,
                in_count               = excluded.in_count,
                out_count              = excluded.out_count,
                hitl_created_count     = excluded.hitl_created_count,
                hitl_assigned_count    = excluded.hitl_assigned_count,
                hitl_replied_count     = excluded.hitl_replied_count,
                hitl_resolved_count    = excluded.hitl_resolved_count
            """,
            (row.project_id, row.day_utc, row.tracker_type, row.model_name,
             row.prompt_tokens_total, row.completion_tokens_total, row.cost_usd_total,
             row.wasted_cost_usd, row.call_count, row.in_count, row.out_count,
             row.hitl_created_count, row.hitl_assigned_count, row.hitl_replied_count,
             row.hitl_resolved_count),
        )
```

### `UsageDailySummaryRepository.query` — new signature (vs skeleton)

The 14.01 skeleton has `query(*, project_id, from_day, to_day)` — Story 14.05 **replaces** that with the correct signature. The skeleton test `test_daily_summary_repo_query_not_implemented` must be updated to `test_daily_summary_repo_query_is_implemented` using the new parameter names.

New signature:
```python
def query(
    self,
    *,
    project_id: int,
    from_day_utc: str,
    to_day_utc: str,
    trackers: list[str] | None = None,
) -> list[UsageDailySummaryRow]:
```

SQL: `SELECT … FROM usage_daily_summary WHERE project_id=? AND day_utc BETWEEN ? AND ?` + optional `AND tracker_type IN (…)` clause if trackers are specified. Results ordered by `(day_utc, tracker_type, model_name)`.

The `query_wasted` skeleton method remains `raise NotImplementedError` — it is Story 14.07 scope.

### `purge_before` — bounded batch delete

```python
def purge_before(self, cutoff_iso: str, batch_size: int = 10_000) -> int:
    deleted = 0
    while True:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "DELETE FROM usage_llm_calls WHERE id IN "
                "(SELECT id FROM usage_llm_calls WHERE created_at < ? LIMIT ?)",
                (cutoff_iso, batch_size),
            )
            n = cur.rowcount
        deleted += n
        if n < batch_size:
            break
    return deleted
```

Use `DELETE … WHERE id IN (SELECT id … LIMIT ?)` — SQLite does not support `LIMIT` directly on `DELETE` unless compiled with `SQLITE_ENABLE_UPDATE_DELETE_LIMIT`. Using the subquery approach works on all SQLite versions. Each batch commits separately (one `with sqlite3.connect(…) as conn:` block per iteration).

### Rollup aggregation SQL — LLM tracker

```sql
SELECT
    model_name,
    COALESCE(SUM(prompt_tokens), 0)      AS prompt_tokens_total,
    COALESCE(SUM(completion_tokens), 0)  AS completion_tokens_total,
    SUM(cost_usd)                         AS cost_usd_total,
    SUM(CASE WHEN call_outcome IN
        ('verifier_rejected','guardrails_blocked','error')
        THEN cost_usd ELSE 0 END)         AS wasted_cost_usd,
    COUNT(*)                              AS call_count
FROM usage_llm_calls
WHERE project_id = ? AND DATE(created_at) = ?
GROUP BY model_name
```

Use `SUM(CASE WHEN …)` rather than `SUM(…) FILTER (WHERE …)` for broader SQLite version compatibility (per story implementation note). One `UsageDailySummaryRow` per returned model_name row.

### Rollup aggregation SQL — messages tracker

```sql
SELECT
    SUM(CASE WHEN direction='in' THEN 1 ELSE 0 END)  AS in_count,
    SUM(CASE WHEN direction='out' THEN 1 ELSE 0 END) AS out_count,
    COUNT(*)                                           AS call_count
FROM usage_messages
WHERE project_id = ? AND DATE(created_at) = ?
```

One row per `(project, day, 'messages', '')`. `model_name = ''` (empty-string sentinel).

### Rollup aggregation SQL — HITL tracker

```sql
SELECT
    SUM(CASE WHEN event_type='created'  THEN 1 ELSE 0 END) AS hitl_created_count,
    SUM(CASE WHEN event_type='assigned' THEN 1 ELSE 0 END) AS hitl_assigned_count,
    SUM(CASE WHEN event_type='replied'  THEN 1 ELSE 0 END) AS hitl_replied_count,
    SUM(CASE WHEN event_type='resolved' THEN 1 ELSE 0 END) AS hitl_resolved_count,
    COUNT(*)                                                 AS call_count
FROM usage_hitl_events
WHERE project_id = ? AND DATE(created_at) = ?
```

One row per `(project, day, 'hitl', '')`. If this query returns 0 rows (no HITL activity), produce no summary row (empty day convention).

### Empty day check — produce no row

For each tracker, check if the aggregation returns any data before calling `upsert`. If COUNT(*) = 0 (or the query returns no rows), skip the `upsert`. This keeps `usage_daily_summary` sparse.

### Project discovery

```sql
SELECT DISTINCT project_id FROM usage_llm_calls
UNION
SELECT DISTINCT project_id FROM usage_messages
UNION
SELECT DISTINCT project_id FROM usage_hitl_events
```

Run this once per rollup invocation to get the set of active projects.

### Catchup logic

```python
from datetime import date, timedelta

def _days_to_rollup(last_summary_day: str | None, yesterday: date, max_days: int = 30) -> list[str]:
    """Return list of YYYY-MM-DD strings from (last+1) to yesterday, capped at max_days."""
    if last_summary_day is None:
        start = yesterday  # no history — only roll up yesterday
    else:
        start = date.fromisoformat(last_summary_day) + timedelta(days=1)
    # Cap at max_days: don't go earlier than yesterday - (max_days - 1)
    earliest = yesterday - timedelta(days=max_days - 1)
    start = max(start, earliest)
    days = []
    d = start
    while d <= yesterday:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days
```

To find `last_summary_day` per project: `SELECT MAX(day_utc) FROM usage_daily_summary WHERE project_id = ?`. This is a raw SQL query inside the rollup module — no need for a dedicated repo method.

### `scheduler_runs.json` format and location

```json
{
    "last_rollup_date_utc": "2026-06-11",
    "last_retention_date_utc": "2026-06-11"
}
```

Location: `settings.usage_db_path` parent directory + `scheduler_runs.json`, e.g. `.data/scheduler_runs.json`. Read on `UsageRollupJob.__init__` (or lazily on first `run()`). Write after a successful run. If the file is missing or malformed (JSON decode error), treat as "no prior run" — both jobs run immediately.

### Due-check logic for `UsageRollupJob`

```python
def _is_due(self, now: datetime) -> bool:
    today_utc = now.date()
    rollup_hour = settings.usage_daily_rollup_hour_utc
    # Due if: (a) today's rollup time has passed, AND (b) we haven't run today yet
    rollup_time_today = datetime(today_utc.year, today_utc.month, today_utc.day,
                                  rollup_hour, 5, 0, tzinfo=UTC)  # +5 minute offset
    if now < rollup_time_today:
        return False
    return self._last_run_date is None or self._last_run_date < today_utc
```

`UsageRetentionJob` uses the same pattern but reads `last_retention_date_utc`.

### Repos bundle passed to run_rollup / run_retention

Create a simple dataclass or namedtuple for the repos bundle:

```python
@dataclass
class RollupRepos:
    llm: UsageLlmCallRepository
    messages: UsageMessageRepository
    hitl: UsageHitlEventRepository
    summary: UsageDailySummaryRepository
    db_path: str  # needed for raw SQL queries (project discovery, last-day lookup)
```

The rollup module makes raw SQL queries (project discovery, per-project last summary day, aggregations) directly on `db_path`. All of those use `sqlite3.connect(db_path)` directly since they aren't encapsulated in existing repository methods. This is acceptable because aggregation queries are read-only and rollup-specific — they don't belong in the repo layer.

### `test_usage_repositories_skeleton.py` updates

Two tests must be updated:
1. `test_daily_summary_repo_upsert_not_implemented` → `test_daily_summary_repo_upsert_is_implemented`: call `repo.upsert(row)` and assert no exception.
2. `test_daily_summary_repo_query_not_implemented` → `test_daily_summary_repo_query_is_implemented`: call `repo.query(project_id=1, from_day_utc="2026-06-01", to_day_utc="2026-06-11")` and assert it returns an empty list (no rows in fresh DB).

`test_daily_summary_repo_query_wasted_not_implemented` remains — `query_wasted` is NOT implemented in this story.

**Also**: `test_llm_call_repo_list_for_day_not_implemented` and similar `count_for_day` tests remain as-is — `list_for_day()` and `count_for_day()` are NOT implemented in this story. The rollup does its aggregation via direct SQL, not through those methods.

### `services/scheduler/app/jobs/__init__.py`

Already exists (from `ProactiveFollowupJob`). No changes needed.

### Wiring in `main.py`

`_build_jobs()` currently returns only `[ProactiveFollowupJob(...)]`. Extend it:

```python
from services.scheduler.app.jobs.usage_rollup_job import UsageRollupJob
from services.scheduler.app.jobs.usage_retention_job import UsageRetentionJob
from services.api.app.usage.repositories import (
    UsageLlmCallRepository, UsageMessageRepository,
    UsageHitlEventRepository, UsageDailySummaryRepository,
)
from services.api.app.usage.migrations import bootstrap_usage_db

def _build_jobs() -> list[Any]:
    ...
    usage_db = settings.usage_db_path
    bootstrap_usage_db(usage_db)  # no-op after first boot
    rollup_repos = RollupRepos(
        llm=UsageLlmCallRepository(db_path=usage_db),
        messages=UsageMessageRepository(db_path=usage_db),
        hitl=UsageHitlEventRepository(db_path=usage_db),
        summary=UsageDailySummaryRepository(db_path=usage_db),
        db_path=usage_db,
    )
    state_path = str(Path(usage_db).parent / "scheduler_runs.json")
    return [
        ProactiveFollowupJob(...),
        UsageRollupJob(repos=rollup_repos, clock=_default_clock, state_path=state_path),
        UsageRetentionJob(repos=rollup_repos, clock=_default_clock, state_path=state_path,
                          retention_days=settings.usage_raw_retention_days,
                          batch_size=settings.usage_rollup_batch_size),
    ]
```

Both jobs share the same `state_path` and `rollup_repos` bundle. `UsageRetentionJob` runs after `UsageRollupJob` in the registered order (runner runs jobs sequentially per tick).

### Testing approach

**`test_usage_rollup_aggregation.py`**: Use `tmp_path` + `bootstrap_usage_db()`, seed raw rows directly with `sqlite3`, then call `await run_rollup(clock=lambda: ..., repos=rollup_repos)` directly (not via the job wrapper). Assert `usage_daily_summary` rows. All async tests use `@pytest.mark.asyncio`.

**`test_scheduler_loop_due_check.py`**: Test `UsageRollupJob.run()` with a monkeypatched clock. Due-check is tested by advancing the clock past rollup time and asserting `run_rollup` is called (mock it). Not-due: assert `run_rollup` NOT called.

**Integration test `test_scheduler_runs_rollup_and_retention.py`**: Use `tmp_path`, real repos, real rollup + retention functions. Seed yesterday's raw rows → run rollup → assert summary exists → advance clock 31 days → run retention → assert raw rows purged but summary intact.

**E2E**: `@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-05")`. Uses injected clock to simulate day boundaries.

### Coverage notes

- `scheduler_runs.json` missing / malformed path must be explicitly tested
- Empty-day skip (no `upsert` call when no data) must be tested
- Batch loop termination condition (last batch returns < batch_size) must be tested
- `RollupRepos` is a dataclass — constructor is covered by instantiation

## Dev Agent Record

### Completion Notes

(to be filled)

### Debug Log

(to be filled)

## File List

**New files:**
- `services/scheduler/app/usage_rollup.py`
- `services/scheduler/app/usage_retention.py`
- `services/scheduler/app/jobs/usage_rollup_job.py`
- `services/scheduler/app/jobs/usage_retention_job.py`
- `tests/test_usage_daily_summary_repository.py`
- `tests/test_usage_rollup_aggregation.py`
- `tests/test_usage_retention.py`
- `tests/test_scheduler_loop_due_check.py`
- `tests/test_scheduler_runs_rollup_and_retention.py`
- `tests/e2e/test_e2e_epic14_rollup_and_retention.py`

**Modified files:**
- `services/api/app/usage/repositories.py`
- `services/scheduler/app/main.py`
- `platform_common/settings.py`
- `.env.example`
- `tests/test_usage_repositories_skeleton.py`

## Change Log

- 2026-06-12: Story 14.05 created (ready-for-dev).
