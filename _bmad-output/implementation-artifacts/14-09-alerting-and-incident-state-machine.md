---
story_key: 14-09-alerting-and-incident-state-machine
epic: 14
story: 9
title: "Alerting (triple-indicator) + incident state machine"
status: ready-for-dev
created: 2026-06-13
---

# Story 14.09 — Alerting (triple-indicator) + incident state machine

## Status

ready-for-dev

## Story

**As an** admin,
**I want** to be DM'd ONCE when a cost / volume / HITL spike starts, told what's expanding when more trackers join, and told when the incident ends — not pinged 24 times a day,
**So that** alerts stay actionable and I trust the channel.

PRD reference: **FR-35** (Triple-Indicator Alerting), **FR-36** (Incident Grouping), Epic 02 carry-forward.

## Acceptance Criteria

1. `UsageIncidentRepository.start()` INSERTs a new incident row with `breached_trackers` as a JSON array and returns the new `id`.
2. `UsageIncidentRepository.expand()` appends a tracker to `breached_trackers` JSON (idempotent — no duplicate); updates `peak_pct` if new value is higher.
3. `UsageIncidentRepository.end()` sets `ended_at` + `total_excess_cost_usd` and returns the closed `UsageIncidentRow`.
4. `UsageIncidentRepository.active_for_project()` returns the open incident (`ended_at IS NULL`) or `None` when none exists.
5. Daily budget cap alert fires when today's LLM cost ≥ `cap_warning_pct`% of cap (default 80%) → `budget_cap_warning`; ≥ `cap_breach_pct`% (default 100%) → `budget_cap_breach`. Already-active incident with the tracker in `breached_trackers` suppresses re-firing.
6. Per-message outlier fires inside the recorder consumer when `SUM(cost_usd) WHERE trace_id=?` exceeds `outlier_per_message_usd` (default $1.00). Fires AFTER writing the row. Same `trace_id` does not fire twice.
7. Rolling-avg rule fires when today's value > `rolling_avg_pct`% (default 200%) of 7-day avg AND above the absolute floor. Gate: skip if project has < 7 prior days of summary data ("day 8+" rule).
8. Incident state machine: any threshold fire → `active_for_project()`. If None → `start()` new incident + `INCIDENT_START` DM. If active → `expand()` + `INCIDENT_EXPAND` DM.
9. On each scheduler tick, for each active incident: if ALL `breached_trackers` are currently below threshold for ≥ `hysteresis_minutes` continuous minutes (default 60) → `end()` + `INCIDENT_END` DM.
10. `INCIDENT_END` DM includes duration, peak %, and total excess cost.
11. Every threshold fire also calls Epic 02 `IncidentRepository.ingest()` with fingerprint `usage:<project_id>:<usage_incident_id>`, severity (`warning` / `critical`), and a `append_event()` for START / EXPAND / END phases.
12. Alert DM strings live in `data/russian_alert_strings.json` (Russian-first content-is-data pattern).
13. `CREATE INDEX IF NOT EXISTS usage_llm_calls_trace_id_idx ON usage_llm_calls (trace_id)` added idempotently to `bootstrap_usage_db()`.
14. 100% line coverage on all new/modified files; `ruff check .` passes.

## Tasks / Subtasks

- [ ] **Task 1: Fix `UsageIncidentRepository` stub signatures and implement**
  - [ ] 1a. Refactor `start()` signature from `(self, row: UsageIncidentRow)` to keyword-args form; implement INSERT; return `lastrowid`.
  - [ ] 1b. Refactor `expand()` signature; implement idempotent JSON-array update + peak_pct logic.
  - [ ] 1c. Refactor `end()` signature; implement UPDATE + fetch + return `UsageIncidentRow`.
  - [ ] 1d. Implement `active_for_project()` — SELECT WHERE `ended_at IS NULL LIMIT 1`.
  - [ ] 1e. Write `tests/test_usage_incident_repository.py` (start, expand idempotency, expand updates peak, end, active, active returns None when closed).

- [ ] **Task 2: Add `usage_llm_calls_trace_id_idx` to migration**
  - [ ] 2a. Add `CREATE INDEX IF NOT EXISTS usage_llm_calls_trace_id_idx ON usage_llm_calls (trace_id)` in `bootstrap_usage_db()`.
  - [ ] 2b. Confirm the migration test (or existing bootstrap tests) still pass.

- [ ] **Task 3: Create `data/russian_alert_strings.json`**
  - [ ] 3a. Write the three alert templates (INCIDENT_START, INCIDENT_EXPAND, INCIDENT_END).

- [ ] **Task 4: Create `services/scheduler/app/alert_formatter.py`**
  - [ ] 4a. Load strings from `data/russian_alert_strings.json` at import time.
  - [ ] 4b. `format_start(*, project_id, tracker, peak_pct) -> str`.
  - [ ] 4c. `format_expand(*, project_id, new_tracker) -> str`.
  - [ ] 4d. `format_end(*, project_id, duration_str, peak_pct, total_excess_usd) -> str`.
  - [ ] 4e. `tests/test_alert_formatter.py` — all three render correctly; bad key raises KeyError.

- [ ] **Task 5: Check / extend `platform_common/settings.py`**
  - [ ] 5a. Add `incidents_db_path: str` with default `.data/semantaix_incidents.db` if not already present.
  - [ ] 5b. Add `admin_alert_chat_id: int | None = None` (alerts disabled if not set).
  - [ ] 5c. Add `usage_daily_budget_cap_usd: float = 0.0` (0 = cap check disabled).

- [ ] **Task 6: Create `services/scheduler/app/usage_alerts.py`**
  - [ ] 6a. Define `AlertConfig` dataclass with all threshold fields + defaults.
  - [ ] 6b. Define `AlertRepos` dataclass (summary_repo, llm_repo, message_repo, hitl_repo, usage_incident_repo).
  - [ ] 6c. Implement `_today_live_llm_cost(*, project_id, day_utc, llm_repo) -> float` — add a `sum_cost_for_day` method to `UsageLlmCallRepository` (direct SUM SQL).
  - [ ] 6d. Implement `_rolling_avg_llm_cost(*, project_id, today_utc, summary_repo) -> float | None` — returns `None` if < 7 prior days.
  - [ ] 6e. Implement `_rolling_avg_messages(*)` and `_rolling_avg_hitl(*)` with the same day-gate logic.
  - [ ] 6f. Implement `_fire_breach(*, tracker, project_id, peak_pct, usage_incident_repo, incident_engine, telegram_sender, hysteresis_state, admin_chat_id, clock)` — encapsulates START-or-EXPAND logic.
  - [ ] 6g. Implement `_check_end_conditions(*, active_incident, repos, alert_config, hysteresis_state, telegram_sender, incident_engine, admin_chat_id, now)`.
  - [ ] 6h. Implement `async def run_alerts(*, clock, repos, telegram_sender, incident_engine, alert_config, hysteresis_state, admin_chat_id, project_ids, daily_budget_caps)`.
  - [ ] 6i. Write `tests/test_usage_alerts_budget_cap.py` (below 80% → no fire; at 80% → START; at 100% → EXPAND if warning active; duplicate tick → no re-fire).
  - [ ] 6j. Write `tests/test_usage_alerts_rolling_avg.py` (day 7 → skip; day 8 below threshold → no fire; day 8 above threshold AND above floor → fire).
  - [ ] 6k. Write `tests/test_usage_alerts_state_machine.py` (START DM; EXPAND DM 30 min later; 59 min under threshold → no END; 60 min under → END DM; re-breach after END opens new incident).

- [ ] **Task 7: Add per-message outlier check to `UsageRecorder`**
  - [ ] 7a. Locate `services/api/app/usage/recorder.py`; after recording a new LLM row (if `trace_id` is set), SUM `cost_usd WHERE trace_id=?` via a new `sum_trace_cost(trace_id)` method on `UsageLlmCallRepository`.
  - [ ] 7b. If sum > outlier threshold AND no active incident with `cost_outlier_per_message` in `breached_trackers` → call outlier-fire path.
  - [ ] 7c. Inject `usage_incident_repo` + `telegram_sender` + `incident_engine` + `alert_config` into `UsageRecorder.__init__`; update lifespan wiring in `services/api/app/usage/lifespan.py`.
  - [ ] 7d. Write `tests/test_usage_alerts_outlier_per_message.py` (3 rows sharing trace_id totalling $1.20 → fire; sum < $1 → no fire; second row on same trace after breach → no duplicate).

- [ ] **Task 8: Create `services/scheduler/app/jobs/usage_alerts_job.py`**
  - [ ] 8a. Follow `UsageRollupJob` pattern: `name = "usage_alerts"`, `__init__`, `_is_due()` (every tick — alerts run every scheduler cycle), `async def run()`.
  - [ ] 8b. Hold `hysteresis_state: dict` on the job instance (persists across ticks in the same process).

- [ ] **Task 9: Wire `UsageAlertsJob` in `services/scheduler/app/main.py`**
  - [ ] 9a. Import `TelegramBotSender` from `services.api.app.telegram_bot_sender`.
  - [ ] 9b. Import `IncidentRepository` from `services.api.app.incidents`.
  - [ ] 9c. Import `UsageIncidentRepository`.
  - [ ] 9d. In `_build_jobs()`: instantiate `telegram_sender`, `incident_engine`, `usage_incident_repo`, build `AlertRepos`, build `AlertConfig`, build `UsageAlertsJob`; add to returned list.
  - [ ] 9e. Guard: if `settings.admin_alert_chat_id is None`, log a warning and skip the `UsageAlertsJob`.

- [ ] **Task 10: Integration test**
  - [ ] 10a. Write `tests/test_alerting_loop_integration.py` — seed summaries + raw rows; advance injected clock; assert DM call order START → EXPAND → END via mocked `TelegramBotSender`.

- [ ] **Task 11: Update story file + sprint status**
  - [ ] 11a. Mark all tasks `[x]`, set `status: done` in this file.
  - [ ] 11b. Update `_bmad-output/implementation-artifacts/sprint-status.yaml`: `14-09-alerting-and-incident-state-machine: done`.

## Dev Notes

### Repository stubs — signatures must change (no existing callers)

The current stubs in [`services/api/app/usage/repositories.py`](services/api/app/usage/repositories.py) (lines 432–446) use placeholder signatures. **Refactor all four** before implementing:

```python
# CURRENT STUBS (wrong — change these)
def start(self, row: UsageIncidentRow) -> int:           # takes a full row — BAD (no id yet)
def expand(self, *, incident_id: int, additional_trackers: str) -> None:  # wrong param names
def end(self, *, incident_id: int, ended_at: str) -> None:  # missing total_excess; no return
def active_for_project(self, *, project_id: int) -> UsageIncidentRow | None:  # correct

# TARGET SIGNATURES
def start(self, *, project_id: int, started_at: str, breached_tracker: str,
          peak_pct: float | None = None) -> int: ...

def expand(self, *, incident_id: int, new_breached_tracker: str,
           new_peak_pct: float | None = None) -> None: ...

def end(self, *, incident_id: int, ended_at: str,
        total_excess_cost_usd: float | None = None) -> UsageIncidentRow: ...

def active_for_project(self, *, project_id: int) -> UsageIncidentRow | None: ...
```

**`start()` implementation sketch:**
```python
def start(self, *, project_id: int, started_at: str, breached_tracker: str,
          peak_pct: float | None = None) -> int:
    import json
    with sqlite3.connect(self._db_path) as conn:
        cur = conn.execute(
            "INSERT INTO usage_incidents (project_id, started_at, breached_trackers, peak_pct)"
            " VALUES (?, ?, ?, ?)",
            (project_id, started_at, json.dumps([breached_tracker]), peak_pct),
        )
        return cur.lastrowid  # type: ignore[return-value]
```

**`expand()` must be idempotent:**
```python
def expand(self, *, incident_id: int, new_breached_tracker: str,
           new_peak_pct: float | None = None) -> None:
    import json
    with sqlite3.connect(self._db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT breached_trackers, peak_pct FROM usage_incidents WHERE id=?",
            (incident_id,),
        ).fetchone()
        trackers: list[str] = json.loads(row["breached_trackers"])
        if new_breached_tracker not in trackers:
            trackers.append(new_breached_tracker)
        current_peak: float | None = row["peak_pct"]
        if new_peak_pct is not None and (current_peak is None or new_peak_pct > current_peak):
            current_peak = new_peak_pct
        conn.execute(
            "UPDATE usage_incidents SET breached_trackers=?, peak_pct=? WHERE id=?",
            (json.dumps(trackers), current_peak, incident_id),
        )
```

**`end()` returns the closed row** (needed for DM rendering):
```python
def end(self, *, incident_id: int, ended_at: str,
        total_excess_cost_usd: float | None = None) -> UsageIncidentRow:
    with sqlite3.connect(self._db_path) as conn:
        conn.execute(
            "UPDATE usage_incidents SET ended_at=?, total_excess_cost_usd=? WHERE id=?",
            (ended_at, total_excess_cost_usd, incident_id),
        )
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, project_id, started_at, ended_at, breached_trackers,"
            "       peak_pct, total_excess_cost_usd"
            " FROM usage_incidents WHERE id=?",
            (incident_id,),
        ).fetchone()
    return UsageIncidentRow(**dict(row))
```

### `UsageLlmCallRepository` — add two helper methods

Add to `UsageLlmCallRepository` in `repositories.py`:

```python
def sum_cost_for_day(self, *, project_id: int, day_utc: str) -> float:
    start_ts = f"{day_utc}T00:00:00Z"
    end_ts = f"{(date.fromisoformat(day_utc) + timedelta(days=1)).isoformat()}T00:00:00Z"
    with sqlite3.connect(self._db_path) as conn:
        val = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage_llm_calls"
            " WHERE project_id=? AND created_at >= ? AND created_at < ?",
            (project_id, start_ts, end_ts),
        ).fetchone()[0]
    return float(val)

def sum_trace_cost(self, *, trace_id: str) -> float:
    with sqlite3.connect(self._db_path) as conn:
        val = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) FROM usage_llm_calls WHERE trace_id=?",
            (trace_id,),
        ).fetchone()[0]
    return float(val)
```

### Epic 02 incident engine integration

`IncidentRepository` lives in `services/api/app/incidents.py`. The scheduler service opens `semantaix_incidents.db` **directly** — same read-write pattern as the usage db (the db file is on a shared Docker volume):

```python
from services.api.app.incidents import IncidentRepository
incident_engine = IncidentRepository(
    db_path=settings.incidents_db_path,
    dedup_window_seconds=300,
)
```

Usage pattern in the alerting code:

```python
# On INCIDENT_START
epic02_incident = incident_engine.ingest(
    fingerprint=f"usage:{project_id}:{usage_incident_id}",
    severity="warning",  # or "critical" for budget_cap_breach
    summary=f"Usage alert: {tracker} breached for project {project_id}",
)
incident_engine.append_event(
    incident_id=epic02_incident.id,
    event_type="INCIDENT_START",
    details=json.dumps({"tracker": tracker, "peak_pct": peak_pct}),
)

# On INCIDENT_EXPAND
incident_engine.append_event(
    incident_id=epic02_incident.id,
    event_type="INCIDENT_EXPAND",
    details=json.dumps({"new_tracker": new_tracker}),
)

# On INCIDENT_END
incident_engine.resolve(epic02_incident.id)
incident_engine.append_event(
    incident_id=epic02_incident.id,
    event_type="INCIDENT_END",
    details=json.dumps({"duration_seconds": ..., "peak_pct": ..., "excess_usd": ...}),
)
```

The fingerprint `usage:<project_id>:<usage_incident_id>` lets the Epic 02 UI track one Epic-02 incident per usage incident lifecycle.

**Finding the Epic 02 incident id for EXPAND/END**: store a mapping `{usage_incident_id: epic02_incident_id}` in memory alongside the hysteresis dict. Or, use `incident_engine.get_by_fingerprint(f"usage:{project_id}:{usage_incident_id}")` on each tick.

### `settings.incidents_db_path` — check if it exists

Search `platform_common/settings.py`:
```bash
grep -n "incidents_db_path\|incidents.db" platform_common/settings.py
```

If absent, add:
```python
incidents_db_path: str = ".data/semantaix_incidents.db"
```

Also verify `init_schema` from `incidents.py` is called on the path before any writes. The api service calls it at startup; the scheduler should call it too if opening the db directly:
```python
from services.api.app.incidents import init_schema
init_schema(settings.incidents_db_path)
```

### TelegramBotSender — scheduler imports it directly

`services/api/app/telegram_bot_sender.py` is importable from the scheduler:

```python
from services.api.app.telegram_bot_sender import TelegramBotSender

telegram_sender = TelegramBotSender(
    bot_token=settings.telegram_bot_token,
    base_url="https://api.telegram.org",
    http_transport=None,
)
```

The DM target (`admin_alert_chat_id`) comes from `settings.admin_alert_chat_id`. Check `platform_common/settings.py` — there may be an existing `hitl_primary_operator_chat_id` or `admin_chat_id` field. If so, reuse it; do not add a duplicate. The story planning says "admin Telegram username + chat_id is in hitl_runtime_config" but for 14.09 we use a Settings env var — config surface is 14.10's job.

### Rolling-avg math

For today's date, `today_utc = "2026-06-13"`, the 7-day window is days `D-7` through `D-1`:

```python
from datetime import date, timedelta

def _rolling_avg_llm_cost(*, project_id: int, today_utc: str,
                           summary_repo: UsageDailySummaryRepository) -> float | None:
    today = date.fromisoformat(today_utc)
    window_start = (today - timedelta(days=7)).isoformat()
    window_end = (today - timedelta(days=1)).isoformat()
    rows = summary_repo.query(
        project_id=project_id,
        from_day_utc=window_start,
        to_day_utc=window_end,
        trackers=["llm"],
        include_money=True,
    )
    if len(rows) < 7:
        return None  # "day 8+" gate — not enough history
    costs = [r.cost_usd_total or 0.0 for r in rows]
    return sum(costs) / len(costs)
```

### `_discover_project_ids` reuse

Already in `services/scheduler/app/usage_rollup.py`:

```python
from services.scheduler.app.usage_rollup import _discover_project_ids
```

The alerter calls `_discover_project_ids(db_path)` to enumerate projects. Alternatively, the alerter passes `project_ids` explicitly (allowing tests to inject a list).

### Alerting tick structure

```python
# services/scheduler/app/usage_alerts.py

async def run_alerts(
    *,
    clock: Callable[[], datetime],
    repos: AlertRepos,
    telegram_sender: TelegramBotSender,
    incident_engine: IncidentRepository,
    alert_config: AlertConfig,
    hysteresis_state: dict[int, datetime | None],
    epic02_id_map: dict[int, int],  # {usage_incident_id: epic02_incident_id}
    admin_chat_id: int,
    project_ids: list[int],
    daily_budget_caps: dict[int, float],
) -> None:
    now = clock()
    today_utc = now.date().isoformat()
    for project_id in project_ids:
        cap = daily_budget_caps.get(project_id, 0.0)
        if cap > 0:
            await _check_budget_cap(...)
        await _check_rolling_avg(...)
    # check for incidents ready to end
    for project_id in project_ids:
        active = repos.usage_incident.active_for_project(project_id=project_id)
        if active:
            await _check_end_condition(active=active, now=now, ...)
```

### In-memory hysteresis state design

```python
# On UsageAlertsJob.__init__:
self._hysteresis: dict[int, datetime | None] = {}
# {usage_incident_id: under_threshold_since_ts OR None if currently over threshold}
```

End-condition check per active incident:
```python
def _all_under_threshold(incident: UsageIncidentRow, repos, alert_config, today_utc, now) -> bool:
    import json
    trackers = json.loads(incident.breached_trackers)
    for tracker in trackers:
        if _is_currently_breached(tracker, incident.project_id, repos, alert_config, today_utc):
            return False
    return True

# In the end-check loop:
incident_id = active.id
if _all_under_threshold(active, ...):
    if hysteresis_state.get(incident_id) is None:
        hysteresis_state[incident_id] = now
    elif (now - hysteresis_state[incident_id]) >= timedelta(minutes=alert_config.hysteresis_minutes):
        closed = repos.usage_incident.end(incident_id=incident_id, ended_at=now.isoformat(), ...)
        # DM + Epic02 resolve + clear hysteresis
        hysteresis_state.pop(incident_id, None)
else:
    hysteresis_state[incident_id] = None  # reset timer on any breach
```

**Crash safety note** (document in module docstring): on scheduler restart, `hysteresis_state` is empty. Active incidents in DB re-start their 60-min timer on the next tick. Worst case is a 60-min delay in closing. This is acceptable — the alternative (persisting to JSON file) adds complexity without meaningful benefit.

### Alert strings format

```json
{
  "incident_start": "⚠️ INCIDENT START\nПроект: {project_id}\nПробит порог: {tracker} ({peak_pct}%)",
  "incident_expand": "⚠️ INCIDENT EXPAND\nПроект: {project_id}\nЕщё пробит порог: {new_tracker}",
  "incident_end": "✅ INCIDENT END\nПроект: {project_id}\nДлительность: {duration}\nПик: {peak_pct}%\nИзлишек: ${total_excess:.2f}"
}
```

Load once at module level in `alert_formatter.py`:
```python
import json, pathlib
_DATA = json.loads((pathlib.Path(__file__).parent.parent.parent / "data/russian_alert_strings.json").read_text())
```

Wait — `services/scheduler/app/alert_formatter.py` is in the scheduler service. The `data/` directory should be at the **repo root** (same as `data/russian_slang.json`, `data/russian_hedges.txt`, etc.). Use:
```python
_DATA_PATH = pathlib.Path(__file__).resolve().parents[3] / "data" / "russian_alert_strings.json"
```

Verify by running: `python3 -c "import pathlib; print(pathlib.Path('services/scheduler/app/alert_formatter.py').resolve().parents[3] / 'data')"`.

### Severity mapping

| Tracker | Epic 02 `severity` |
|---------|--------------------|
| `budget_cap_breach` | `"critical"` |
| `budget_cap_warning` | `"warning"` |
| `cost_outlier_per_message` | `"warning"` |
| `llm_cost_rolling_avg_breach` | `"warning"` |
| `messages_rolling_avg_breach` | `"warning"` |
| `hitl_rolling_avg_breach` | `"warning"` |

### `UsageRecorder` outlier check wiring

The recorder lives in `services/api/app/usage/recorder.py`. The outlier check fires after `self._llm_repo.record(row)`:

```python
if row.trace_id:
    trace_total = self._llm_repo.sum_trace_cost(trace_id=row.trace_id)
    if trace_total > self._alert_config.outlier_per_message_usd:
        active = self._usage_incident_repo.active_for_project(project_id=row.project_id)
        already_fired = (
            active is not None
            and "cost_outlier_per_message" in json.loads(active.breached_trackers)
        )
        if not already_fired:
            await self._fire_breach(
                tracker="cost_outlier_per_message",
                project_id=row.project_id,
                peak_pct=None,
                ...
            )
```

The recorder's `__init__` gains: `usage_incident_repo`, `telegram_sender`, `incident_engine`, `alert_config`, `admin_chat_id`. Update `services/api/app/usage/lifespan.py` wiring accordingly.

**Note**: The recorder's consumer runs in `asyncio.to_thread` for the DB write, then fires the outlier check in the event loop. The `_fire_breach` helper must be async.

### Scheduler job pattern (for UsageAlertsJob)

Follow `UsageRollupJob` exactly:

```python
# services/scheduler/app/jobs/usage_alerts_job.py
class UsageAlertsJob:
    name = "usage_alerts"

    def __init__(self, *, repos, clock, telegram_sender, incident_engine,
                 alert_config, admin_chat_id, project_ids, daily_budget_caps):
        self._repos = repos
        self._clock = clock
        self._telegram_sender = telegram_sender
        self._incident_engine = incident_engine
        self._alert_config = alert_config
        self._admin_chat_id = admin_chat_id
        self._project_ids = project_ids
        self._daily_budget_caps = daily_budget_caps
        self._hysteresis: dict[int, datetime | None] = {}
        self._epic02_id_map: dict[int, int] = {}

    def _is_due(self, now: datetime) -> bool:
        return True  # run every tick

    async def run(self) -> None:
        await run_alerts(
            clock=self._clock,
            repos=self._repos,
            telegram_sender=self._telegram_sender,
            incident_engine=self._incident_engine,
            alert_config=self._alert_config,
            hysteresis_state=self._hysteresis,
            epic02_id_map=self._epic02_id_map,
            admin_chat_id=self._admin_chat_id,
            project_ids=self._project_ids,
            daily_budget_caps=self._daily_budget_caps,
        )
```

### Wiring in `services/scheduler/app/main.py`

```python
from services.api.app.incidents import IncidentRepository, init_schema as init_incidents_schema
from services.api.app.telegram_bot_sender import TelegramBotSender
from services.api.app.usage.repositories import UsageIncidentRepository
from services.scheduler.app.jobs.usage_alerts_job import UsageAlertsJob
from services.scheduler.app.usage_alerts import AlertConfig, AlertRepos

# In _build_jobs(), after existing job setup:
if settings.admin_alert_chat_id is not None:
    init_incidents_schema(settings.incidents_db_path)
    telegram_sender = TelegramBotSender(
        bot_token=settings.telegram_bot_token,
        base_url="https://api.telegram.org",
        http_transport=None,
    )
    incident_engine = IncidentRepository(
        db_path=settings.incidents_db_path,
        dedup_window_seconds=300,
    )
    usage_incident_repo = UsageIncidentRepository(db_path=usage_db)
    alert_repos = AlertRepos(
        summary=rollup_repos.summary,
        llm=rollup_repos.llm,
        message=rollup_repos.messages,
        hitl=rollup_repos.hitl,
        usage_incident=usage_incident_repo,
    )
    project_ids = _discover_project_ids(usage_db)
    daily_budget_caps = {pid: settings.usage_daily_budget_cap_usd for pid in project_ids}
    jobs.append(UsageAlertsJob(
        repos=alert_repos,
        clock=_default_clock,
        telegram_sender=telegram_sender,
        incident_engine=incident_engine,
        alert_config=AlertConfig(),
        admin_chat_id=settings.admin_alert_chat_id,
        project_ids=project_ids,
        daily_budget_caps=daily_budget_caps,
    ))
else:
    logger.warning("admin_alert_chat_id not set — usage alerts disabled")
```

### Previous story learnings (14.07)

- **100% coverage gate**: every branch of every new function needs a test. Coverage misses on `if not already_fired` branches will fail CI.
- **SQLite `lastrowid`**: available on the cursor returned by `conn.execute("INSERT ...")`. Return `int(cur.lastrowid)` — type checkers may flag it as `int | None`.
- **asyncio in tests**: use `pytest-asyncio` + `@pytest.mark.asyncio` for async test functions. Check `.coveragerc` includes scheduler paths.
- **`AsyncMock` for `TelegramBotSender.send_message`**: `send_message` is `async def`; patch with `AsyncMock` from `unittest.mock`.
- **Avoid global SQLite state**: each test function creates its own `tmp_path / "u.db"` and calls `bootstrap_usage_db`.

### Files to create / modify

| Action | File |
|--------|------|
| MODIFY | `services/api/app/usage/repositories.py` — fix 4 stubs, add `sum_cost_for_day`, `sum_trace_cost` |
| MODIFY | `services/api/app/usage/migrations.py` — add `usage_llm_calls_trace_id_idx` |
| MODIFY | `services/api/app/usage/recorder.py` — add outlier check post-write |
| MODIFY | `services/api/app/usage/lifespan.py` — wire outlier deps into `UsageRecorder` |
| MODIFY | `platform_common/settings.py` — add `incidents_db_path`, `admin_alert_chat_id`, `usage_daily_budget_cap_usd` if absent |
| MODIFY | `services/scheduler/app/main.py` — wire `UsageAlertsJob` in `_build_jobs()` |
| CREATE | `data/russian_alert_strings.json` |
| CREATE | `services/scheduler/app/alert_formatter.py` |
| CREATE | `services/scheduler/app/usage_alerts.py` |
| CREATE | `services/scheduler/app/jobs/usage_alerts_job.py` |
| CREATE | `tests/test_usage_incident_repository.py` |
| CREATE | `tests/test_usage_alerts_budget_cap.py` |
| CREATE | `tests/test_usage_alerts_outlier_per_message.py` |
| CREATE | `tests/test_usage_alerts_rolling_avg.py` |
| CREATE | `tests/test_usage_alerts_state_machine.py` |
| CREATE | `tests/test_alert_formatter.py` |
| CREATE | `tests/test_alerting_loop_integration.py` |

## Dev Agent Record

### Debug Log

_(empty — story not yet started)_

### Completion Notes

_(empty)_

## File List

_(to be filled in by dev agent on completion)_

## Change Log

_(to be filled in by dev agent on completion)_
