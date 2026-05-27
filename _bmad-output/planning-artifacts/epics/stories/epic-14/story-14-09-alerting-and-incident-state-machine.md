# Story 14.09 — Alerting (triple-indicator) + incident state machine (`INCIDENT_START → EXPAND → END`)

## Objective
Ship cost-spike alerting with three indicators (daily budget cap, per-message outlier, rolling-avg day 8+) and replace per-hour throttling with an **incident state machine** in `usage_incidents`: `INCIDENT_START` (first threshold cross while no active incident) → `INCIDENT_EXPAND` (additional tracker breaches) → `INCIDENT_END` (all breached trackers under threshold for ≥60 min continuous). Alerts emit through the Epic 02 incident engine with fingerprint `usage:<project_id>:<incident_id>` so existing Alerts-tab UI + `@ajdevy` Telegram notifications surface them without bespoke channels. Channel: admin Telegram DM only.

**As an** admin,
**I want** to be DM'd ONCE when a cost / volume / HITL spike starts, told what's expanding when more trackers join, and told when the incident ends — not pinged 24 times a day,
**So that** alerts stay actionable and I trust the channel.

PRD reference: **FR-35** (Triple-Indicator Alerting), **FR-36** (Incident Grouping), Epic 02 carry-forward.

## Scope

### In Scope
- **`UsageIncidentRepository.start(*, project_id, started_at, breached_tracker, peak_pct) -> int`** — INSERTs a new incident row; returns the new id.
- **`UsageIncidentRepository.expand(*, incident_id, new_breached_tracker, new_peak_pct) -> None`** — UPDATEs `breached_trackers` JSON to add the new tracker; updates `peak_pct` if `new_peak_pct` is higher.
- **`UsageIncidentRepository.end(*, incident_id, ended_at, total_excess_cost_usd) -> UsageIncidentRow`** — UPDATEs `ended_at` + final fields; returns the closed row for DM rendering.
- **`UsageIncidentRepository.active_for_project(project_id) -> UsageIncidentRow | None`** — returns the active incident (if any) for a project.
- **`UsageIncidentRepository.list_for_window(project_id, from_iso, to_iso) -> list[UsageIncidentRow]`** — used by `/api/usage/incidents` (14.07 returns from here).
- **`services/scheduler/app/usage_alerts.py`** — the alerting tick:
  - Entry point `async def run_alerts(*, clock, repos, telegram_sender, incident_engine, alert_config) -> None`. Wired into the scheduler loop (runs every ~60s alongside roll-up / retention).
  - **Indicator 1 — Daily budget cap** (per project, always active):
    - For each project, fetch today's `cost_usd_total` from `usage_daily_summary` (or sum live `usage_llm_calls` rows if today's summary hasn't been rolled up yet — use the live raw rows for today, summary rows for prior days).
    - Compare to `daily_budget_cap_usd` (from `hitl_runtime_config[project].usage_daily_budget_cap_usd`).
    - If today's cost ≥ 80% of cap AND no `budget_cap_warning` fired today for this project → fire `budget_cap_warning`.
    - If today's cost ≥ 100% of cap AND no `budget_cap_breach` fired today → fire `budget_cap_breach`.
    - "Already fired today" tracked via the incident state machine (an open incident with `breached_trackers` containing `budget_cap_warning` / `budget_cap_breach` suppresses re-firing).
  - **Indicator 2 — Per-message cost outlier** (always active):
    - Triggered DURING LLM-call instrumentation (14.02), NOT in the scheduler loop. When `usage_llm_calls` records a new row, the recorder (or a downstream observer) checks: does the sum of `cost_usd` for all rows sharing this `trace_id` exceed $1.00? If yes AND no `cost_outlier` incident for this trace already → fire `cost_outlier_per_message`.
    - **Implementation choice**: do this in the recorder's consumer task AFTER the row is written — SELECT `SUM(cost_usd) WHERE trace_id=?` and compare. Fast (indexed by `trace_id` for this purpose — add `usage_llm_calls_trace_id_idx` in this story).
  - **Indicator 3 — Rolling-avg rule** (day 8+):
    - For each project, compute the 7-day rolling avg of yesterday's-and-prior `cost_usd_total` (LLM), `(in_count + out_count)` (messages), and total HITL events.
    - If today's value > 200% of avg AND > the absolute floor → fire the respective `*_rolling_avg_breach`.
    - "Day 8+" means: count days since the project's first activity row in any tracker. <8 days → skip rolling-avg check.
  - **Incident state machine (FR-36)**:
    - On any fire: check `active_for_project(project_id)`. If none → `start` a new incident with this tracker breach; DM `INCIDENT_START`. If active → `expand` and DM `INCIDENT_EXPAND`.
    - On each scheduler tick: for each active incident, check if ALL its `breached_trackers` are CURRENTLY under threshold. If yes AND the under-threshold state has held for ≥60 min continuous → `end` the incident; DM `INCIDENT_END` with duration + peak % + total excess cost.
    - "Under threshold continuous for 60 min" — track via a small in-memory state `{incident_id: under_threshold_since_ts}`; if a breach re-occurs mid-window, reset to None.
- **Alert DM rendering** in `services/scheduler/app/alert_formatter.py`:
  - `INCIDENT_START` Russian template: `"⚠️ INCIDENT START\nПроект: <name>\nПробит порог: <tracker> (<peak_pct>%)"`.
  - `INCIDENT_EXPAND`: `"⚠️ INCIDENT EXPAND\nПроект: <name>\nЕщё пробит порог: <new_tracker>"`.
  - `INCIDENT_END`: `"✅ INCIDENT END\nПроект: <name>\nДлительность: <duration>\nПик: <peak_pct>%\nИзлишек: $<total_excess>"`.
  - Strings live in `data/russian_alert_strings.json`.
- **Telegram delivery channel** — DM to `@ajdevy` (or whatever the admin Telegram username + chat_id is in `hitl_runtime_config`). Reuse the existing `TelegramBotSender` via the scheduler service's existing service-to-service path to bot_gateway (or directly if scheduler has a Telegram client now; check existing pattern).
- **Epic 02 incident-engine integration** — every threshold fire ALSO emits to the existing `incidents` table with fingerprint `usage:<project_id>:<incident_id>`, severity per indicator (`warning` for budget-cap warning + rolling-avg + outlier; `critical` for budget-cap breach), and `incident_events` timeline rows for START / EXPAND / END. This way the Alerts tab in Web UI shows usage incidents alongside other system incidents.
- **`hitl_runtime_config` schema additions** — per-project keys (free-form key/value table):
  - `usage_daily_budget_cap_usd` (required at project creation)
  - `usage_cap_warning_pct` (default 80)
  - `usage_cap_breach_pct` (default 100)
  - `usage_outlier_per_message_usd` (default 1.0)
  - `usage_rolling_avg_pct` (default 200)
  - `usage_rolling_avg_floor_llm_usd` (default 3.0)
  - `usage_rolling_avg_floor_messages` (default 50)
  - `usage_rolling_avg_floor_hitl` (default 20)
  - `usage_incident_hysteresis_minutes` (default 60)
- **Per-project alert config admin UI / slash command** — defer to 14.10 (signoff covers config UI + backfill).

### Out of Scope
- The web UI banner for active incidents (Alerts tab inherits them from Epic 02 — no new banner).
- Kill-switch / auto-pause LLM calls when over budget (out — brainstorm H4 "notify only, no kill-switch").
- Per-user / per-conversation incident attribution (project-scoped only).
- Multi-project incident rollup (one project per incident).
- Alert delivery channels beyond admin Telegram DM (no email, no webhook).
- Per-project alert config UI (14.10 owns the admin-facing config surface; this story uses defaults + reads `hitl_runtime_config` if values exist).

## Implementation Notes
- **Outlier-detection placement** — putting it in the recorder consumer (rather than the scheduler loop) means a per-message outlier fires within seconds of the offending message, not on the next scheduler tick. The trade-off: the recorder consumer carries one extra SQL query per LLM-call write. With the `usage_llm_calls_trace_id_idx` index, this is sub-millisecond. Worth it.
- **`usage_llm_calls_trace_id_idx`** — added to this story's migration (idempotent `CREATE INDEX IF NOT EXISTS`). Filed against the existing 14.01 migration module.
- **Rolling-avg math** — `SELECT AVG(cost_usd_total) FROM usage_daily_summary WHERE project_id=? AND tracker_type='llm' AND day_utc >= ? AND day_utc < ?` over the 7 prior days. Skip if < 7 days of data (the "day 8+" gate).
- **In-memory under-threshold-since state** — keep a dict `{incident_id: datetime_or_None}` in the scheduler process. On scheduler restart, the in-memory state is lost — the scheduler re-evaluates from scratch on the next tick (worst case: an active incident never closes from a stale prior breach; but each tick checks "are all breached trackers currently under threshold?" — once true, the timer starts anew). Document this in a module-level docstring. **Crash-safety trade-off**: we COULD persist `under_threshold_since` to a JSON file, but the recovery is naturally self-correcting in 60-90 minutes; not worth the storage complexity.
- **Daily budget cap backfill** — existing projects need a default cap at Epic 14 release. Story 14.10 owns the backfill + admin notification.
- **Alerting on the rollup-not-yet-complete moment** — for today's data, query live raw rows (not the daily-summary which doesn't exist for today yet). For yesterday + prior, query summaries. Build a small "today's live total" helper that the alerter shares with the bot `/usage` command's "today" path.
- **Idempotent fires** — the state machine inherently makes fires idempotent: `INCIDENT_START` only fires if no active incident; `INCIDENT_EXPAND` only adds to `breached_trackers` if the tracker isn't already present.
- **Threshold-cross detection** — the alerter compares CURRENT VALUES to thresholds. If a tracker was already "above threshold" on the previous tick, that's continuity, not a new fire. The state machine prevents duplicate DMs because the incident already lists this tracker in `breached_trackers`.
- **Test-friendly: inject the clock + the Telegram sender + the incident engine**. All collaborators are constructor-injected per project-context rule.
- **Severity mapping** — `budget_cap_breach` → `critical`; `budget_cap_warning` + `cost_outlier_per_message` + all `*_rolling_avg_breach` → `warning`.
- **Russian alert strings** — `data/russian_alert_strings.json` follows the Russian-first-content-is-DATA pattern.

## Test Plan

### Unit
- `tests/test_usage_incident_repository.py`:
  - `start` → returns new id with `breached_trackers=['budget_cap_warning']` (JSON-encoded).
  - `expand` → adds a tracker to the JSON; updates `peak_pct` if new is higher.
  - `expand` with a tracker already in the list → no-op (no duplicate).
  - `end` → sets `ended_at` and final fields; returns the closed row.
  - `active_for_project` → returns active row when `ended_at IS NULL`; returns None when closed.
- `tests/test_usage_alerts_budget_cap.py`:
  - Cap = $10, today's cost = $7.99 → no fire.
  - Cap = $10, today's cost = $8.01 → `budget_cap_warning` fires; incident_start emitted; DM rendered.
  - Cap = $10, today's cost = $10.01 → `budget_cap_breach` fires (after warning already fired earlier in the day, this is INCIDENT_EXPAND).
  - Same conditions on next tick → no duplicate fires (state machine suppression).
- `tests/test_usage_alerts_outlier_per_message.py`:
  - Three LLM calls share trace_id `T1`, costs `[0.40, 0.50, 0.30]` → sum = $1.20 → outlier fires.
  - Same trace_id with sum < $1 → no fire.
  - Fire happens AFTER the third row is written (recorder consumer test).
- `tests/test_usage_alerts_rolling_avg.py`:
  - Day 7 with `today_cost=$15`, `7d_avg=$5` → no fire (bootstrap gate).
  - Day 8 with same values → `llm_cost_rolling_avg_breach` fires.
  - Day 8 with `today_cost=$10`, `7d_avg=$5` (200% but only $5 increase, below $3 floor — wait, $5 > $3 floor, this WOULD fire). Reword: Day 8 with `today_cost=$7.50`, `7d_avg=$5` (150% — below 200%) → no fire.
  - Day 8 with `today_cost=$5.50`, `7d_avg=$2` (275% AND $3.50 increase) → fires.
- `tests/test_usage_alerts_state_machine.py`:
  - Cost breach fires → `INCIDENT_START` DM.
  - Messages breach 30 min later (active incident exists) → `INCIDENT_EXPAND` DM.
  - All trackers return under threshold for 59 min → no end DM yet.
  - At 60 min continuous under threshold → `INCIDENT_END` DM with duration ≈ 90 min, peak ≈ <observed peak>, total excess cost.
  - New cost breach after END → opens a NEW incident (`INCIDENT_START` again).
- `tests/test_alert_formatter.py`:
  - All three DM templates render correctly with synthetic input; Russian strings load from JSON file.

### Contract
- `tests/contract/test_usage_alerts_incidents_engine_payload.py` — assert the payload emitted to the Epic 02 incident engine has the correct fingerprint (`usage:<project_id>:<incident_id>`), severity, and `incident_events` timeline shape.

### Integration
- `tests/test_alerting_loop_integration.py` — boot the scheduler with seeded summaries + raw rows; advance the injected clock; assert DMs are sent via the mocked Telegram sender in the right order: START → EXPAND → END.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_alerting_lifecycle.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-09")`):
  - Synthetic-traffic project with cap = $10; inject LLM calls totaling $8.01 → assert exactly ONE `budget_cap_warning` DM.
  - Continue injecting until $10.01 → assert exactly ONE `INCIDENT_EXPAND` (or `budget_cap_breach` STARTING a new incident, depending on the state machine's choice — verified by the spec).
  - Inject a single LLM call with $1.50 cost on one trace_id → assert `cost_outlier_per_message` fires.
  - Multi-tracker breach (LLM + messages 30 min apart) → ONE START + ONE EXPAND DM.
  - All trackers return under threshold; advance clock 60 min → ONE END DM with the right duration / peak / excess cost.
  - Stay under threshold for 30 min, breach again, then under for 60 min straight → the under-threshold-since timer resets correctly; eventual END fires after the right window.

## Manual Verification
1. Set a project's daily cap to $1 (a low value); send synthetic traffic that costs > $0.80 → confirm `INCIDENT_START` DM arrives in `@ajdevy`'s Telegram.
2. Push cost over $1 → confirm `INCIDENT_EXPAND` (or `INCIDENT_START` if cap_warning hadn't fired).
3. Stop traffic; wait 60 min → confirm `INCIDENT_END` DM with duration + peak + excess.
4. Confirm the Alerts tab in `/admin/alerts` shows the incident with fingerprint `usage:<project_id>:<incident_id>` and a timeline of START / EXPAND / END events.

## Done Criteria
- 100% line coverage on `usage_alerts.py`, `alert_formatter.py`, the new `UsageIncidentRepository` methods, the recorder's outlier-check addition.
- `ruff check .` passes.
- All three indicators verified by tests (budget cap, outlier, rolling avg with bootstrap gate).
- State machine verified — single START + single EXPAND + single END per lifecycle.
- 60-min hysteresis verified.
- Epic 02 incident engine integration verified — usage incidents appear in Alerts tab.
- Russian alert strings live in `data/russian_alert_strings.json`.
- E2E alerting lifecycle green.
- Project-config schema additions in `hitl_runtime_config` documented (config UI deferred to 14.10).
