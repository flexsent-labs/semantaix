# Story 14.05 — Daily roll-up worker + 30-day raw retention purge (scheduler)

## Objective
Promote the `scheduler` service from heartbeat placeholder to its first real workload: a daily job that (1) aggregates raw rows from `usage_llm_calls`, `usage_messages`, `usage_hitl_events` into `usage_daily_summary` keyed `(project_id, day_utc, tracker_type, model_name)`, (2) computes `wasted_cost_usd` per LLM-summary row (sum over `call_outcome ∈ {verifier_rejected, guardrails_blocked, error}`), and (3) purges raw rows older than 30 days. Idempotent UPSERT semantics; raw rows kept 30 days rolling; daily summaries kept forever.

**As an** admin,
**I want** the dashboard, alerts, and `/usage` command to be fast and accurate by reading pre-aggregated daily summaries instead of scanning 30 days of raw rows for every chart tick,
**So that** WAL contention stays low and queries respond in milliseconds even as the project scales toward 100k LLM calls/day.

PRD reference: **FR-31** (Daily Roll-up + 30-Day Raw Retention), **NFR-10** (Storage Scale).

## Scope

### In Scope
- **`UsageDailySummaryRepository.upsert(*, project_id, day_utc, tracker_type, model_name, **counters) -> None`** implementation:
  - UPSERT on the PK `(project_id, day_utc, tracker_type, model_name)`. `model_name = ''` empty-string sentinel for non-LLM rows (per 14.01 convention).
  - Counter columns set from kwargs: `prompt_tokens_total`, `completion_tokens_total`, `cost_usd_total`, `wasted_cost_usd` (LLM only — NULL for non-LLM), `call_count`, `in_count`, `out_count`, `hitl_created_count`, `hitl_assigned_count`, `hitl_replied_count`, `hitl_resolved_count`.
  - Implemented as `INSERT … ON CONFLICT(project_id, day_utc, tracker_type, model_name) DO UPDATE SET …`.
- **`UsageDailySummaryRepository.query(*, project_id, from_day_utc, to_day_utc, trackers: list[str]) -> list[UsageDailySummaryRow]`** — read path for the dashboard / `/usage` / alerting; returns rows for the window + trackers, ordered by `(day_utc, tracker_type, model_name)`.
- **`services/scheduler/app/usage_rollup.py`** — the rollup job:
  - Entry point `async def run_rollup(*, clock, repos) -> None`. Wired into the scheduler service's loop (which Epic 14 will refactor from heartbeat to "tick once every N seconds; run scheduled jobs whose schedule is due").
  - **Schedule**: runs every UTC midnight + a configurable offset (`USAGE_DAILY_ROLLUP_HOUR_UTC`, default `0` meaning 00:05 UTC; small offset ensures all of yesterday's writes have flushed). Actual run cadence: the scheduler loop checks every 60s whether the next-due roll-up time has passed.
  - **Day-by-day catchup**: on startup, queries `usage_daily_summary` for the most recent `day_utc` per project. For each project, runs the roll-up for every day between (most-recent-day + 1) and yesterday. This way a scheduler outage doesn't lose days (idempotent UPSERT means re-running is safe).
  - **Aggregation logic per day per project**:
    - LLM tracker: `SELECT model_name, SUM(prompt_tokens), SUM(completion_tokens), SUM(cost_usd), SUM(cost_usd) FILTER (WHERE call_outcome IN ('verifier_rejected','guardrails_blocked','error')), COUNT(*) FROM usage_llm_calls WHERE project_id=? AND DATE(created_at)=? GROUP BY model_name`. One summary row per `(project, day, llm, model)`.
    - Messages tracker: `SELECT SUM(direction='in'), SUM(direction='out'), COUNT(*) FROM usage_messages WHERE project_id=? AND DATE(created_at)=?`. One summary row per `(project, day, messages, '')`.
    - HITL tracker: `SELECT event_type, COUNT(*) FROM usage_hitl_events WHERE project_id=? AND DATE(created_at)=? GROUP BY event_type`. One summary row per `(project, day, hitl, '')` with the four counts.
  - **Empty days produce no summary rows** (sparse table per FR-31 acceptance criterion).
  - Structured logs: `usage_rollup_started` (with `from_day_utc`, `to_day_utc`, `project_count`), `usage_rollup_day_completed` (with `day_utc`, `project_id`, `tracker_type`, `rows_written`), `usage_rollup_completed` (with `total_summary_rows`).
- **`services/scheduler/app/usage_retention.py`** — the 30-day raw purge:
  - Entry point `async def run_retention(*, clock, repos, retention_days: int = 30) -> None`. Wired into the scheduler loop alongside `run_rollup` (runs after roll-up completes, so summaries for purged days exist before raw rows go).
  - **Purge logic**: `DELETE FROM usage_llm_calls WHERE created_at < ?`; same for `usage_messages` and `usage_hitl_events`. The cutoff is `(clock() - timedelta(days=retention_days)).isoformat(timespec='seconds') + 'Z'`.
  - **Bounded delete**: use `LIMIT 10000` per query in a `while`-loop until all matched rows are gone — avoids long-held WAL locks on a very large `.data/semantaix_usage.db`.
  - Structured logs: `usage_retention_started`, `usage_retention_purged` (per table, with `rows_deleted`), `usage_retention_completed`.
- **`UsageLlmCallRepository.purge_before(...)`**, **`UsageMessageRepository.purge_before(...)`**, **`UsageHitlEventRepository.purge_before(...)`** methods to back the retention job. Each takes `(cutoff_iso, batch_size=10000)` and returns `rows_deleted`.
- **Scheduler service refactor (light-touch)** — `services/scheduler/app/main.py` (heartbeat placeholder) gains a minimal loop:
  ```
  async def scheduler_main():
      while True:
          await maybe_run_usage_rollup()
          await maybe_run_usage_retention()
          await asyncio.sleep(60)
  ```
  - `maybe_run_*` reads the last-run timestamp from an in-memory state (or a tiny `scheduler_runs.json` file in `.data/` for crash safety) and runs only when due.
  - Existing health-check + heartbeat surfaces unchanged.
- **New `Settings` fields** in `platform_common/settings.py`:
  - `usage_daily_rollup_hour_utc: int = 0` (the hour at which yesterday's roll-up runs; with a 5-minute offset baked into the loop check).
  - `usage_raw_retention_days: int = 30`.
  - `usage_rollup_batch_size: int = 10000`.
  - Add to `.env.example`.

### Out of Scope
- Hourly roll-ups (intentionally dropped per brainstorm C5).
- Dashboard, API read endpoints, `/usage` bot command (14.06–14.08).
- Alerting (14.09 — the alerting job uses these summaries but adds its own scheduled task).
- Backfill of historical data (none — fresh start per FR-31).
- Real-time roll-up (out — daily granularity only; raw rows cover sub-day spike debugging).

## Implementation Notes
- **Idempotent UPSERT** — re-running for the same `(project, day, tracker, model)` produces identical row values. Tests run the roll-up twice and assert no diff.
- **Day boundary in UTC** — `DATE(created_at)` works because `created_at` is stored as UTC ISO-8601 (`...Z` suffix). For the dashboard, browser-tz rendering re-buckets in JS (handled in 14.06); the roll-up stays UTC.
- **Day-by-day catchup** — on scheduler boot, find each project's most recent summary day and run roll-up forward day-by-day until yesterday. Hard-cap the catchup at 30 days (since raw rows older than that are purged, older summaries can't be reconstructed; if the scheduler is down for 31+ days, days beyond 30 just stay absent from summaries — accepted limitation).
- **Project discovery** — `SELECT DISTINCT project_id FROM usage_llm_calls UNION SELECT DISTINCT project_id FROM usage_messages UNION SELECT DISTINCT project_id FROM usage_hitl_events`. Projects with zero activity get no summary rows.
- **Bounded delete** — the retention purge uses `DELETE … WHERE created_at < ? LIMIT 10000` and loops. Each batch commits, releasing the WAL lock between rounds. At 100k calls/day/project × 30 days = 3M rows max per table — bounded retention prevents indefinite growth.
- **`wasted_cost_usd`** — computed via `SUM(cost_usd) FILTER (WHERE call_outcome IN ('verifier_rejected','guardrails_blocked','error'))` in the same aggregation pass. SQLite 3.30+ supports `FILTER` syntax; if the deployed SQLite is older, fall back to `SUM(CASE WHEN call_outcome IN (...) THEN cost_usd ELSE 0 END)`.
- **`cost_usd_total`** — `SUM(cost_usd)` over all rows for the day-model bucket. NULL `cost_usd` rows contribute 0 (SQLite `SUM` ignores NULLs). If ALL rows for a bucket have NULL `cost_usd`, the total is NULL (not 0); the dashboard renders that as `—`.
- **Scheduler crash-safety** — `scheduler_runs.json` stores `{last_rollup_at_utc, last_retention_at_utc}`. On boot, read it; if missing, default to "run roll-up now if not done for today" (which is idempotent so it's safe).
- **Structured logging hygiene** — log volumes per project; never log individual `cost_usd` values in the roll-up job (admin-scope logs are fine but keep volume low).

## Test Plan

### Unit
- `tests/test_usage_daily_summary_repository.py`:
  - `upsert` insert path → row exists; second `upsert` with different counters for the same PK → UPDATE (no second row).
  - `query` filtered by `trackers=['llm']` returns only LLM rows; `trackers=['messages','hitl']` returns the other two trackers.
  - `model_name` empty-string vs non-empty: querying for `model_name=''` returns non-LLM rows only.
- `tests/test_usage_rollup_aggregation.py`:
  - Seed `usage_llm_calls` with 3 rows for project 1, day `2026-05-25`, model `claude-haiku-4-5`, mixed `call_outcome` → `run_rollup` produces ONE summary row with the correct totals, including `wasted_cost_usd` = sum over `verifier_rejected`/`guardrails_blocked`/`error` rows.
  - Multiple models on the same day → one summary row PER MODEL.
  - Empty day for a project → no summary row.
  - Re-running for the same day is idempotent (zero new rows, same values).
  - Day-by-day catchup: most recent summary is `2026-05-20`, today is `2026-05-26` → roll-up runs for days 21, 22, 23, 24, 25 (not today).
- `tests/test_usage_retention.py`:
  - Seed raw rows with `created_at` spanning 60 days → `run_retention(retention_days=30)` deletes rows older than the cutoff; rows from the last 30 days are intact.
  - Bounded batch: seed 25000 rows older than cutoff → the purge runs 3 batches (10k, 10k, 5k); each batch logs `usage_retention_purged` with `rows_deleted`.
  - `usage_daily_summary` rows for purged days are NOT deleted (forever retention).
- `tests/test_scheduler_loop_due_check.py`:
  - `maybe_run_usage_rollup` runs when current UTC time has crossed `USAGE_DAILY_ROLLUP_HOUR_UTC + 5min` and the last-run was on a prior day.
  - `maybe_run_usage_rollup` does NOT run twice on the same day.
  - `maybe_run_usage_retention` runs after rollup completes (ordering preserved).

### Contract
- N/A.

### Integration
- `tests/test_scheduler_runs_rollup_and_retention.py` — start the scheduler service against a fresh `.data/`; seed raw rows for yesterday across all three trackers; advance the injected clock past midnight + 5 min; assert `usage_daily_summary` rows appear; advance clock 31 days; assert raw rows from day 0 are purged but summary rows remain.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_rollup_and_retention.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-05")`):
  - End-to-end: send synthetic traffic across all three trackers for a project across 3 days; run the roll-up; query the summary table → counts and totals match the synthetic input.
  - Force a scheduler outage (skip the rollup for 5 days), then run → catchup roll-up backfills the missed days correctly.
  - Run retention with `retention_days=2` (test override) → raw rows older than 2 days are gone; summaries remain.

## Manual Verification
1. `docker compose up --build -d`; let it run for a day with synthetic traffic; the next morning, `sqlite3 .data/semantaix_usage.db "SELECT * FROM usage_daily_summary ORDER BY day_utc DESC, tracker_type LIMIT 20;"` shows yesterday's row(s) per tracker.
2. Check scheduler logs → `usage_rollup_completed` and `usage_retention_completed` events present per day.
3. Stop the scheduler for 3 days, restart → confirm catchup runs in scheduler logs; summary rows for the missed days appear.
4. With raw data older than 30 days seeded, run retention → confirm purge logs + counts; `sqlite3 .data/semantaix_usage.db "SELECT COUNT(*) FROM usage_llm_calls WHERE created_at < datetime('now', '-30 days');"` returns 0.

## Done Criteria
- 100% line coverage on `usage_rollup.py`, `usage_retention.py`, scheduler loop changes, and the three new repo methods.
- `ruff check .` passes.
- Roll-up idempotency verified by run-twice test.
- Day-by-day catchup verified.
- Bounded retention purge (batch size) verified.
- Heartbeat / health endpoints on the scheduler service unchanged.
- E2E rollup-and-retention green.
