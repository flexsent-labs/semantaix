# Epic 14: Usage / Token / Cost Monitoring + Cost-Spike Alerting

## Goal
Surface LLM token usage, message volume, and HITL activity per project so admins can see spend over 1d/1w/1m windows in the Web UI and via a bot command, while operators see token/message/HITL volume only (no money) on the same surfaces. Instrumentation runs from scattered call sites through a single async fire-and-forget ingestion seam into a new SQLite store `semantaix_usage.db` (WAL mode). Includes triple-indicator alerting (daily budget cap + per-message outlier + rolling-avg day 8+) with an incident state machine (`INCIDENT_START → INCIDENT_EXPAND* → INCIDENT_END`) that supersedes per-hour throttling. Always-on; **no feature flag**; **no backfill** of historical calls. Reuses the Epic 02 incident engine (fingerprint `usage:<project_id>:<incident_id>`) for the alerting surface; depends on the Epic 10.5 operator/project model refinement for the RBAC + bot stories (14.07 and 14.08); extends the Epic 07 backup runbook to cover the new DB; reads `call_outcome=moderation_triggered` to disambiguate moderator-driven LLM spend from customer-driven spend (Epic 12 story 12-05b 20-material cap already ships the upstream mitigation). Implements PRD **FR-26–FR-36** and **NFR-8–NFR-11**.

## In Scope
- **New SQLite store `semantaix_usage.db` (WAL mode)** with five tables, owned by `api`, RO from `web_ui`, RW from `scheduler`. Schema fixed per FR-26 / PRD §6:
  - `usage_llm_calls` — raw, 30d retention. Columns `project_id, model_name, prompt_tokens, completion_tokens, cost_usd (NULL-tolerant), call_outcome, trace_id, created_at`. Index `(project_id, created_at)`.
  - `usage_messages` — raw, 30d. Columns `project_id, direction (in|out), participant_role (customer|operator), trace_id, created_at`. Index `(project_id, created_at)`.
  - `usage_hitl_events` — raw, 30d. Columns `project_id, event_type (created|assigned|replied|resolved), ticket_id, trace_id, created_at`. Index `(project_id, created_at)`.
  - `usage_daily_summary` — forever. PK `(project_id, day_utc, tracker_type, model_name NULL for non-LLM)`. Columns include `prompt_tokens_total, completion_tokens_total, cost_usd_total, wasted_cost_usd, call_count, in_count, out_count, hitl_event_count_<type>`. Index `(project_id, day_utc, model_name)`.
  - `usage_incidents` — state machine. Columns `id, project_id, started_at, ended_at NULL while active, breached_trackers JSON, peak_pct, total_excess_cost_usd`. Index `(project_id, started_at)`.

- **Idempotent migration + fresh-deploy path** — `sqlite_master` existence check; `PRAGMA table_info` before each `ADD COLUMN`; fresh-deploy creates all five tables directly with the final schema + WAL pragma + indexes. Migration is exercised by an explicit "run twice" test.

- **New `UsageRecorder` ingestion seam** (in `services/api/app/usage/`) with three repository classes (sync `sqlite3`, dispatched via `asyncio.to_thread`): `UsageLlmCallRepository`, `UsageMessageRepository`, `UsageHitlEventRepository`. The seam exposes one signature `record(tracker_type, project_id, payload, *, trace_id)` and dispatches internally — adding a fourth tracker in a future epic requires only a payload shape + table, not a new transport. **Async fire-and-forget** via an `asyncio.Queue` + background consumer task; write failure logs `usage_record_failed` but **never raises** into the caller (NFR-8).

- **`call_outcome` enum** — `customer_visible_answer | verifier_rejected | escalated_to_hitl | guardrails_blocked | moderation_triggered | error`. Each `GroundedRagAnswerer` exit point + KB-upload analyzer + HITL escalation path passes the outcome to the seam.

- **OpenRouter LLM instrumentation** — `openrouter_client.py` (or its successor) captures `usage.prompt_tokens`, `usage.completion_tokens`, `usage.cost` (NULL-tolerant) and `model` from each response. **No self-counting.** Records carry `trace_id` from `AnswerContext`.

- **Scattered call-site outcome reporting** — `GroundedRagAnswerer` reports `customer_visible_answer` on success, `verifier_rejected` after a failed verifier check, `guardrails_blocked` after a regex / profanity reject; HITL escalation path reports `escalated_to_hitl`; KB-upload auto-analyzer (Epic 12) reports `moderation_triggered`; OpenRouter-error path reports `error`.

- **bot_gateway message-volume instrumentation** — one `usage_messages` row per inbound and outbound message, emitted at the bot_gateway message branches.

- **HITL ticket-lifecycle instrumentation** — `hitl.py` emits one `usage_hitl_events` row at each of `created`, `assigned`, `replied`, `resolved` transitions.

- **Scheduler daily roll-up worker** — new `services/scheduler/app/usage_rollup.py` job. Runs after UTC day boundary, aggregates raw rows into `usage_daily_summary` (idempotent UPSERT semantics). Computes `wasted_cost_usd` = sum of `cost_usd` where `call_outcome ∈ {verifier_rejected, guardrails_blocked, error}`. Same job runs **30-day raw retention purge** on `usage_llm_calls`, `usage_messages`, `usage_hitl_events`. Provides the scheduler service its first real workload (previously a heartbeat placeholder).

- **Web UI Usage dashboard** (`/admin/usage`) — single-page; time selector (1d / 1w / 1m / custom range, custom range bounded to ≤30 days for drill-down); per-tracker line/sparkline charts; per-model LLM breakdown; Wasted-spend tile with per-`call_outcome` breakdown chart; drill-down to last-30d raw call list (paginated). **Summary-first reads**: chart + tile queries hit `usage_daily_summary` only; raw rows queried only on drill-down click (mitigates SQLite WAL contention, Attack 14). **Browser-timezone rendering**: days converted in JS on read; storage stays UTC; week starts Monday in local week. "No data yet" empty state per tile; "Usage data unavailable" banner on `usage.db` corruption.

- **New api endpoints** (behind cookie session for browser, `internal_service_token` for bot):
  - `GET /api/usage/summary?project_id=&from=&to=&trackers=` — daily summary rows for the window + trackers.
  - `GET /api/usage/raw?project_id=&day_utc=&tracker_type=` — last-30d drill-down only.
  - `GET /api/usage/wasted?project_id=&from=&to=` — wasted-spend tile (admin only; 403 for operator).
  - `GET /api/usage/incidents?project_id=&from=&to=` — usage-incident list.

- **Money RBAC enforced at API layer** — operator-scope queries physically exclude `cost_usd`, `wasted_cost_usd`, and any other monetary field from the SQL projection (`SELECT` list). Verified by capturing the executed SQL string in a test. Project scoping enforced by Epic 10.5's flat-list-many-operators-per-project / one-project-per-operator mapping.

- **`/usage` bot command** (operator + admin):
  - Admin output: today's LLM cost + tokens + call count + per-model breakdown + Wasted-spend line + Messages block + HITL block + deep link to `/admin/usage`.
  - Operator output: same as admin but with **all cost / wasted-spend fields stripped at the API boundary**; response body byte-clean of "$" and `cost_`/`wasted_` substrings.
  - Project resolved from operator's single project assignment (Epic 10.5); admin can `/usage <project_name>` to scope.
  - Text-only — no chart images. Operator-gated via Epic 10 registry; non-registered senders ignored with logged `unauthorized_usage`, **no DM**.

- **Triple-indicator alerting (per project)**:
  - **Daily budget cap** — admin-set per project at creation (required field; existing projects backfilled at Epic 14 release). Scheduler checks rolling daily LLM cost vs cap; fires `budget_cap_warning` at 80%, `budget_cap_breach` at 100%. Always active including day 1.
  - **Per-message cost outlier** — any single inbound message whose attributable LLM cost (sum of `cost_usd` across `usage_llm_calls` rows sharing the inbound's `trace_id`) exceeds **$1.00** fires `cost_outlier_per_message`. Always active.
  - **Rolling-avg rule** (active **day 8+**): LLM cost > 200% × 7d avg AND > $3 absolute increase → `llm_cost_rolling_avg_breach`; Messages > 200% AND > +50 → `messages_rolling_avg_breach`; HITL > 200% AND > +20 → `hitl_rolling_avg_breach`.
  - All thresholds stored in `hitl_runtime_config`, runtime-configurable per project.

- **Incident grouping state machine (`usage_incidents`)** — `INCIDENT_START` (first breach while no active incident) → `INCIDENT_EXPAND` (additional tracker breaches during active incident; updates `breached_trackers` JSON) → `INCIDENT_END` (all breached trackers under threshold for ≥60 min continuous; message includes duration, peak %, total excess cost). Emits through the Epic-02 `incidents` engine with fingerprint `usage:<project_id>:<incident_id>` so existing dedup/ack/resolve UI surfaces them in the Alerts tab. **Supersedes per-hour throttle (brainstorm H3).**

- **Channel for alert DMs**: admin Telegram DM only (no Web UI banner, no kill-switch — notify only).

- **Backup runbook extension (Epic 07)** — `semantaix_usage.db` added to the tar.gz archive list in `scripts/backup_*.sh` (or its successor). Corruption-simulation test verifies "Usage data unavailable" banner + uninterrupted LLM/message/HITL flows.

- **Per-project alert-threshold UI in web_ui Settings** (or operator/admin slash command) — admin sets daily budget cap, optionally overrides default thresholds. At Epic 14 release, a one-time backfill assigns a default cap to all existing projects (admin notification + 30-day grace window before alerts fire).

- **Settings field** — new `usage_db_path` in `platform_common/settings.py`; new env vars (e.g. `USAGE_DB_PATH`, `USAGE_DAILY_ROLLUP_HOUR_UTC`, `USAGE_RAW_RETENTION_DAYS = 30`) with `.env.example` entries.

- **Structured logging** — new event names: `usage_record_failed`, `usage_rollup_started`, `usage_rollup_completed`, `usage_retention_purged`, `usage_incident_started`, `usage_incident_expanded`, `usage_incident_ended`, `budget_cap_warning`, `budget_cap_breach`, `cost_outlier_per_message`, `llm_cost_rolling_avg_breach`, `messages_rolling_avg_breach`, `hitl_rolling_avg_breach`, `unauthorized_usage`. All carry `trace_id` where applicable; never log `cost_usd` to operator-scope-visible log routes.

- **Acceptance:** PRD FR-26–FR-36 + NFR-8–NFR-11 satisfied; ruff clean; pytest --cov shows 100% line coverage on the new Epic 14 modules; `pytest -m e2e` green for `tests/e2e/test_e2e_epic14_*.py`.

## Out of Scope
- **Backfill of historical LLM calls** from existing `answer_traces` rows — Epic 14 ships fresh-start (brainstorm J1). A future epic may add a one-time backfill if needed.
- **Feature flag** — Epic 14 is always-on (J3); no `usage_enabled` config switch.
- **Multi-currency display** — USD only (brainstorm D1). RUB-paying-client conversion is accepted limitation.
- **Per-user / per-conversation attribution** — attribution unit is **project** for all three trackers (brainstorm B1). User-level usage is not surfaced.
- **Hourly roll-ups** — dropped (C5). 30-day raw retention covers the spike-debugging window.
- **Web UI banner alerts / kill-switch** — channel is admin Telegram DM only (brainstorm H2 / H4).
- **Cost-stuffing protection beyond the daily budget cap** — accepted limitation (brainstorm Attack 6).
- **Audit log of who viewed cost data** — out of scope (brainstorm G4); regular structured logs cover access events.
- **Error-monitoring rollup** — `call_outcome=error` rows are recorded but Epic 14 does not surface an error dashboard; that lives in a future epic (brainstorm A5).
- **Hardening for >100k LLM calls/day/project** — NFR-10 documents this as a "revisit storage choice" trigger; not engineered for in v1.
- **Side-channel cost inference protection** — operators seeing token counts + knowing model rates can estimate cost; accepted at current scale (brainstorm G2 / Attack 4).
- **OpenRouter schema-drift handling** — new `usage` fields (e.g. `cached_tokens`) require a deliberate schema bump in a future epic (brainstorm Attack 7).
- **Privacy-mode for transcript-correlatable usage data** — 30-day token-count metadata + timestamps can be correlated with `story1.db` transcripts; accepted at current scale (brainstorm Attack 11).
- **Cleanup of any deprecated alias** introduced by Epic 14 (none planned in this epic) — N/A.

## Dependencies
- **Epic 01 / pipeline** — `AnswerPipeline`, `AnswerContext.trace_id`, `GroundedRagAnswerer` exit points (instrumentation lives at these seams).
- **Epic 02 — Incident engine** *(carry-forward constraint)* — Usage incidents emit through the existing `incidents` engine with fingerprint `usage:<project_id>:<incident_id>`. Reuses Alerts-tab UI (FR-7) and Telegram critical notifications channel (FR-8). **Mandatory** per the from-Epic-03-onward rule.
- **Epic 04 — HITL lifecycle** — `hitl.py` ticket transitions are the instrumentation source for `usage_hitl_events`. No schema change to HITL tables; Epic 14 only reads / observes.
- **Epic 07 — Backup runbook** — extended in story 14.10 to cover `semantaix_usage.db` (`scripts/backup_*.sh` add path; restore-test extended).
- **Epic 09 — bot_gateway operator command surface** — `/usage` slash command follows the existing dispatch pattern (`_SLASH_RE` anchor + Epic 10 operator-registry gate). `TelegramBotSender` for the deep-link DMs.
- **Epic 10 — Operator registry** — operator-gating for `/usage`. Non-registered senders ignored with logged `unauthorized_usage`.
- **Epic 10.5 — Operator/project model refinement** *(MUST land before stories 14.07 + 14.08 merge)* — removes `hitl_primary_operator_username`; establishes flat-list-many-operators-per-project (one project per operator). RBAC + `/usage` project resolution depend on this model. Stories 14.01–14.06, 14.09 can proceed in parallel; 14.07 and 14.08 are gated.
- **Epic 11 — Calendar instrumentation** — calendar OAuth `freeBusy` calls are NOT OpenRouter calls and do not appear in `usage_llm_calls`. Out of Epic 14 LLM-tracker scope by design.
- **Epic 12 — 20-material cap (story 12-05b, already merged in PR #83)** — Epic 14 reads `call_outcome = moderation_triggered` to disambiguate moderator-driven LLM spend from customer-driven spend.
- **`hitl_runtime_config`** — Epic 14 alert thresholds (daily budget cap, cap-warning/breach percentages, per-message outlier $, rolling-avg coefficients) stored here per the config-in-DB pattern.
- **`platform_common`** — `Settings` for env vars; `app_factory` for new router; structured logging conventions.
- **`scheduler`** — receives its first real workload (daily roll-up + 30d retention purge). Replaces heartbeat placeholder.

## Exit Criteria
- **Migration is idempotent** — running it twice on the same DB is a no-op the second time (no `duplicate column name`, no `no such table`); fresh-deploy path produces all five tables with WAL pragma + indexes; explicit "run twice" test exercises both paths.
- **Three-tracker round-trip:** a customer message that triggers an LLM call results in exactly (1) one row in `usage_llm_calls` with non-NULL `prompt_tokens`/`completion_tokens` and correct `call_outcome` (per the answer's exit point), (2) one `usage_messages` row for the inbound + one for the outbound, (3) zero `usage_hitl_events` rows (no escalation). A customer message that escalates to HITL additionally results in `usage_hitl_events` rows for `created` + `assigned` (and later `replied` + `resolved` after operator turn).
- **Call-outcome fidelity:** synthetic verifier-rejected, guardrails-blocked, KB-upload-analysis, and OpenRouter-error scenarios each produce the correct `call_outcome` value in `usage_llm_calls`.
- **Daily roll-up is idempotent:** re-running the scheduler job for day N produces zero new rows / zero deletions on the second run.
- **30-day raw retention:** raw rows older than 30 days are not visible from any read path; daily summaries from before that window remain queryable.
- **Wasted-spend correctness:** `usage_daily_summary.wasted_cost_usd` for a `(project, day, model)` equals sum over raw rows for the matching key where `call_outcome ∈ {verifier_rejected, guardrails_blocked, error}`.
- **Dashboard summary-first reads:** chart + tile queries hit `usage_daily_summary` only (verified by query-counter test); raw rows queried only on drill-down click.
- **Browser-timezone rendering:** an admin in `Europe/Moscow` viewing the 1d window sees "today" cut at local 00:00 Moscow time (not 00:00 UTC); week starts on the Monday of the local week.
- **Money RBAC byte-clean:** operator-scope `/api/usage/summary` response contains zero `cost_usd` or derived monetary fields; SQL-capture test confirms the `SELECT` list excludes `cost_usd`. Operator `/usage` Telegram output contains zero `$` characters and zero `cost_` / `wasted_` substrings. Operator `/api/usage/wasted` → 403.
- **Triple-indicator alerting fires correctly:** on day 1 with `$10/day` cap, crossing `$8` fires `budget_cap_warning`, crossing `$10` fires `budget_cap_breach`; a `$1.50` aggregate-cost inbound fires `cost_outlier_per_message` exactly once; on day 8, `$15` today vs `$5` 7d-avg fires `llm_cost_rolling_avg_breach`; on day 7 with same data, nothing fires (bootstrap gate).
- **Incident grouping fires once per lifecycle:** a multi-tracker breach (LLM cost > cap, then messages > rolling avg 30 min later) produces ONE `INCIDENT_START` + ONE `INCIDENT_EXPAND` DM; under-threshold for ≥60 min continuous fires ONE `INCIDENT_END` DM with duration + peak % + total excess cost.
- **Epic 02 surfacing:** Usage incidents appear in the Alerts tab with fingerprint `usage:<project_id>:<incident_id>` and a timeline of START / EXPAND / END events.
- **Liveness under usage-DB failure:** simulated `semantaix_usage.db` outage produces no answer-pipeline latency regression, no customer-visible error, no HITL ticket-lifecycle gap; LLM calls + message routing + HITL transitions complete unchanged; dashboard shows "Usage data unavailable" banner instead of crashing.
- **Backup coverage:** `semantaix_usage.db` is included in the tar.gz archive; restore-test recovers daily-summary continuity (raw rows older than 30 days remain purged either way).
- **Epic 10.5 prerequisite:** stories 14.07 (API+RBAC) and 14.08 (`/usage`) merge only after Epic 10.5 is shipped and merged to main.
- **`ruff check .` clean; `pytest --cov` 100% line coverage** on new Epic 14 modules: `services/api/app/usage/recorder.py`, `services/api/app/usage/repositories.py` (or split per-tracker), `services/api/app/usage/api_router.py`, `services/scheduler/app/usage_rollup.py`, `services/scheduler/app/usage_retention.py`, `services/scheduler/app/usage_alerts.py`, `services/scheduler/app/usage_incidents.py`, `services/bot_gateway/app/usage_command.py`, `services/web_ui/app/usage_dashboard.py` (and supporting modules); **`pytest -m e2e` green**.
- **Audit posture:** structured logs cover every threshold-cross + every incident state transition + every usage-write failure; operator-published cost numbers are NEVER logged on operator-scope read routes.

## Automated E2E verification
- Story-aligned tests under `tests/e2e/test_e2e_epic14_*.py` decorated with `@pytest.mark.e2e`, `@pytest.mark.epic("14")`, and per-story `@pytest.mark.story("14-NN")`.
- New scripted signoff: `scripts/epic14_signoff.sh` (CI-parity lint + coverage + `pytest -m e2e` + smoke of the three-tracker round-trip + dashboard render + `/usage` admin/operator output diff + triple-indicator alerting + incident-state-machine lifecycle).
- Matrix updated in `_bmad-output/implementation-artifacts/e2e-coverage.md` with one row per story (14.01–14.10).

## Proposed Story Split

10 stories, each implemented on its own branch and merged via its own PR (per `epics/README.md` "one PR per story" rule). Dependency hints listed; **14.07 and 14.08 are gated on Epic 10.5 shipping first**.

| Story | Title | Depends on | Notes |
|---|---|---|---|
| **14.01** | Schema + base tables in `semantaix_usage.db` | — | Foundation; blocks 14.02–14.09. Idempotent migration + fresh-deploy path; WAL pragma; indexes. |
| **14.02** | OpenRouter LLM-call instrumentation + `call_outcome` enum | 14.01 | Scattered call-site outcome reporting from `GroundedRagAnswerer` exit points + KB-upload analyzer + HITL escalation path + OpenRouter error path. Async fire-and-forget seam (`UsageRecorder`). |
| **14.03** | Message-volume instrumentation (bot_gateway) | 14.01 | One row per inbound/outbound message. |
| **14.04** | HITL event instrumentation (api) | 14.01 | One row per `created \| assigned \| replied \| resolved` transition in `hitl.py`. |
| **14.05** | Daily roll-up worker + 30d raw retention purge (scheduler) | 14.02, 14.03, 14.04 | Idempotent UPSERT into `usage_daily_summary`; `wasted_cost_usd` computation; raw-row purge. Scheduler's first real workload. |
| **14.06** | Web UI dashboard (charts, drill-down, Wasted-spend tile, browser-tz rendering) | 14.05 | Summary-first reads; raw only on drill-down. Empty-state + degraded-state UX. |
| **14.07** | API endpoints + money RBAC (admin vs operator scope) | 14.05, **Epic 10.5** | Money filter enforced at SQL projection; SQL-capture test for byte-cleanness. |
| **14.08** | `/usage` bot command (role-aware, three-tracker output) | 14.07, **Epic 10.5** | Project resolved from operator's single assignment (Epic 10.5); admin can `/usage <project_name>`. |
| **14.09** | Alerting: triple-indicator + incident state machine | 14.05 | Budget cap config + outlier + rolling avg + `INCIDENT_START/EXPAND/END` lifecycle. Emits through Epic 02 engine. |
| **14.10** | Epic signoff: backup runbook update, alert-threshold config UI, e2e tests, project-cap backfill | 14.06, 14.08, 14.09 | Epic 07 runbook extension; one-time backfill of daily budget cap on existing projects (admin notification + 30-day grace window). |

**Critical path:** 14.01 → 14.02 / 14.03 / 14.04 (parallel) → 14.05 → 14.06 / 14.09 (parallel; 14.07 and 14.08 wait on Epic 10.5) → 14.10.

**Estimated duration:** ~6 PRs / 6 dev-stories that can proceed immediately after Epic 10.5 lands (14.01, 14.02, 14.03, 14.04, 14.05, 14.06/14.09), plus 14.07/14.08 gated, plus 14.10 final. With the BMAD one-feature-epic-at-a-time rule and one-PR-per-story rule, Epic 14 is approximately 10 PRs.
