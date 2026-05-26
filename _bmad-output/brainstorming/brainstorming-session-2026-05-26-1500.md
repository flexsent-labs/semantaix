---
stepsCompleted: [1, 2, 3]
inputDocuments: []
session_topic: 'Epic 14 — Usage / Token / Cost Monitoring + Cost-Spike Alerting'
session_goals: 'Produce (a) a shortlist of locked design decisions ready to feed bmad-create-epics-and-stories and (b) an edge-case map covering failure modes, abuse cases, and attribution gotchas.'
selected_approach: 'ai-recommended'
techniques_used: ['Question Storming', 'Reverse Brainstorming + Assumption Reversal']
ideas_generated: ['45 locked decisions', '14 attacks (6 real / 8 noise)', '6 risk mitigations', '2 cross-epic action items', '10-story split proposal']
context_file: ''
technique_execution_complete: true
facilitation_notes: 'User was highly decisive — drove convergent decisions rapidly through Phase 1. Phase 2 attack pass surfaced 6 load-bearing edge cases. Formal Solution Matrix skipped (Phase 3) since trade-off surface was already collapsed; consolidated into final shortlist + risk map instead.'
---

# Brainstorming Session Results

**Facilitator:** Aj
**Date:** 2026-05-26

## Session Overview

**Topic:** Epic 14 — Usage / Token / Cost Monitoring + Cost-Spike Alerting

Surface LLM token usage, message volume, and cost across the bot/api so admins can see per-project spend over 1d/1w/1m windows in the web UI and via a bot command, while operators see token/message volume only (no money) in the same surfaces. Must instrument all current and future LLM call sites (Epic 11 calendar/availability, Epic 12 sales persona auto-analysis, RAG verifier, etc.) from a single chokepoint. Includes alerting when cost spikes against a baseline.

**Goals:**

1. **Shortlist of locked design decisions** — story-level granularity, ready for `bmad-create-epics-and-stories`.
2. **Edge-case map** — failure modes, abuse cases, attribution gotchas, RBAC pitfalls.

## Technique Selection

**Approach:** AI-Recommended Techniques

**Recommended Techniques:**

- **Question Storming** (deep) — Map the full decision surface. Generate questions only, no answers. Output feeds Phase 3.
- **Reverse Brainstorming + Assumption Reversal** (creative + deep) — Build the edge-case map by asking "how could this fail / leak / mislead?" Interleave assumption flips to catch unconscious defaults.
- **Solution Matrix** (structured) — Lock decisions. For each open question from Phase 1, score 2–3 candidate answers against criteria (complexity, blast radius, reversibility, RBAC fit).

**AI Rationale:** User's two goals (decision shortlist + edge-case map) need targeted convergent techniques rather than wide divergence. Sequence mirrors a question→risk→decision funnel that drops cleanly into `bmad-create-epics-and-stories`.

## Technique Execution Results

### Phase 1 — Question Storming (CLOSED)

**Big reframe (load-bearing decision):**

Epic 14 is **three separable trackers**, not one:

1. **OpenRouter LLM tracker** — tokens, cost, model. Money-bearing.
2. **Message-volume tracker** — any in/out message (no LLM tie-in).
3. **HITL tracker** — all HITL events.

All three share `semantaix_usage.db` (one DB, three tables). Daily-summary roll-up runs per-tracker.

---

#### A. Instrumentation
- **A1** Scattered — each answerer/call site reports its own usage to the queue.
- **A2** Token + cost data read from OpenRouter response fields. No self-counting.
- **A3** Only OpenRouter calls land in the LLM tracker.
- **A4** Message volume captured at the **bot_gateway** (closer to wire).
- **A5** Errored LLM calls → separate error-monitoring concern (out of scope for Epic 14).

#### A'. Three-tracker structure (NEW)
- **A'1** One DB (`semantaix_usage.db`), three tables.
- **A'2** Polymorphic intake — single ingestion seam in api with `tracker_type` discriminator.
- **A'3** Each tracker has its own daily summary roll-up.
- **A'4** Bot gateway emits message-volume events; api emits LLM + HITL events.
- **A'5** HITL tracker counts **events** (created/assigned/replied/resolved), not individual messages.

#### B. Attribution
- **B1** Unit = **project** for all three trackers.
- **B2** Only OpenRouter calls in LLM tracker.
- **B3** KB-upload auto-analysis cost → project.
- **B4** HITL-driven LLM calls → project.
- **B5** Zero-LLM messages still count toward **message** tracker (separate concern from LLM tracker).

#### B'. Pricing source assumptions (NEW)
- **B'1** Capture `usage.cost` from OpenRouter response when present; if absent, record token counts only and leave cost field NULL.
- **B'2** No fail on missing cost — record what's available, surface NULL in UI as "—".
- **B'3** Model name captured per row.

#### C. Storage
- **C1** New `semantaix_usage.db`, WAL mode (web_ui reads RO).
- **C2** Raw per-call rows + per-(project,day,model) summaries.
- **C3 (SUPERSEDED)** Raw rows now retained **30 days** (was 24h — changed by E4 drill-down decision). Daily summaries kept forever.
- **C4** Token counts + cost only — no payloads, no payload sizes, no forensics.
- **C5** Hourly rollups dropped — raw 30d covers spike-debugging window. (Pending final confirm.)

#### D. Pricing
- **D1** USD only.
- **D2** Only LLM cost in scope. No Qdrant / infra / TTS costs.

#### E. Web UI
- **E1** Single dashboard (not three tabs).
- **E2** 1d / 1w / 1m windows + custom range.
- **E3** Charts (line/sparkline).
- **E4** Drill-down to call list (works for last 30 days per C3 update).
- **E5** Per-model breakdown — yes.

#### F. Bot commands
- **F1** One role-aware command `/usage`.
- **F2** All three trackers in one output (LLM cost block, message-volume block, HITL block).
- **F3** Output is text + a deep link to web UI.
- **F4** No chart image — text only.

#### G. RBAC
- **G1** Money filter enforced at API layer — operator endpoints never return cost fields.
- **G2** Operators see token + volume counts across all three trackers (no cost only).
- **G3** **Refined model:** a project has many flat-list operators (no "primary"). An operator belongs to exactly one project. (Implication: removes `hitl_primary_operator_username` — Epic 10 amendment, out of scope for Epic 14.)
- **G4** No audit log of who viewed cost data.

#### H. Alerting
- **H1** Hybrid threshold per tracker (200% of 7-day rolling avg AND absolute floor); bootstrap gate (no alerts until ≥7 days history).
  - LLM cost: >200% AND >$3 above avg
  - Messages: >200% AND >+50 above avg
  - HITL: >200% AND >+20 above avg
  - All thresholds are runtime-configurable per-project.
- **H2** Channel = admin Telegram DM.
- **H3** Throttle = every breach, max 1 alert/hour/project/tracker.
- **H4** Notify only, no kill-switch.
- **H5** Alerts cover all three trackers — LLM cost, message volume, HITL volume.

#### I. Timezone & windows
- **I1** 24h = UTC day.
- **I2** Monday week start.
- **I3** "Last 30 days" rolling.

#### J. Migration & rollout
- **J1** Fresh start — no backfill of historical LLM calls.
- **J2** Empty project shows "No data yet" placeholder.
- **J3** Always-on — no feature flag.

#### K. Performance
- **K1** Async queue (fire-and-forget) writes from instrumentation sites.
- **K2** Silent loss on usage write failure (LLM call unaffected).
- **K3** SQLite is fine at this scale with indexes on `(project, created_at)` raw, `(project, day, model)` daily. Revisit if a single project crosses ~100k LLM calls/day.

#### L. Backup / corruption
- **L1** `usage.db` included in Epic 7 backup runbook.
- **L2** On `usage.db` corruption: degrade gracefully — LLM calls continue, dashboard shows "Usage data unavailable".

#### Cross-cutting consequences for other epics
- **Epic 10 amendment needed** — remove `hitl_primary_operator_username`; bot gateway routes to any operator on the project (round-robin? first available? — TBD in that epic). Not Epic 14's job.

---

### Phase 2 — Reverse Brainstorming + Assumption Reversal (IN PROGRESS)

#### Round 1 — 5 attacks

**Attack 1 — REAL — Verifier double-charge / wasted spend invisibility**
- *Threat:* `GroundedRagAnswerer` burns 2-3 LLM calls per inbound (grounded → verifier → optional profanity check). On verifier rejection, no customer-visible answer is produced but cost is incurred. Wasted spend is invisible in current design.
- *Mitigation locked:* `call_outcome` enum column on raw rows (`customer_visible_answer | verifier_rejected | escalated_to_hitl | guardrails_blocked | error`). Daily summary gains `wasted_cost_usd`. Web UI shows "Wasted spend" tile + per-outcome breakdown chart.

**Attack 2 — Noise — Operator-on-one-project transition.** Out of scope for Epic 14; folded into Epic 10 amendment.

**Attack 3 — Noise — Async queue loss on bot restart.** Accepted per K2 ("silent loss on write failure"). Documented as known limitation: bot restart during traffic creates flat-valley gap in LLM cost chart while messages tracker (synchronous via story1.db) keeps recording — produces visible chart asymmetry.

**Attack 4 — Noise — Operator token side-channel disclosure.** Side-channel inference of cost via known model rates is accepted; not a realistic concern at this scale.

**Attack 5 — Partial real — Cold-start false alarms.**
- *Threat:* Bootstrap gate (≥7d history) combined with biased early baseline creates day-8 false alarms; days 1-7 have no alerting at all.
- *Mitigation locked:* Triple-indicator alert structure:
  1. **Daily budget cap** (admin-set per project at creation): alert at 80% spend, escalate at 100% breach. Always active.
  2. **Per-message cost outlier**: any single inbound message > $1.00 fires alert. Always active.
  3. **Rolling-avg rule (H1)**: 200% + absolute floor; active from day 8.

#### Round 2 — 5 attacks

**Attack 6 — Noise — Cost-stuffing by malicious operator.** Budget cap is sufficient defense; intent-based gaming accepted as low-probability for current customer base.

**Attack 7 — Noise — OpenRouter schema drift.** Out of scope. Captured `usage.cost`/`prompt_tokens`/`completion_tokens` are the only fields tracked; future fields (cached_tokens, etc.) require a deliberate schema bump.

**Attack 8 — REAL — Cascading alert storm.**
- *Threat:* H3 (1 alert/hour) produces up to 24 alerts/day during a multi-tracker breach. Admin alert fatigue.
- *Mitigation locked:* Replace H3 throttle with **incident-grouped alerting**:
  - `INCIDENT START` — first threshold cross while no active incident.
  - `INCIDENT EXPAND` — additional tracker breaches during an active incident.
  - `INCIDENT END` — all breached trackers under threshold for ≥60 min continuous. Summary includes duration, peak %, total excess cost.
  - Storage: new `usage_incidents` table in `semantaix_usage.db`. Schema: project_id, started_at, ended_at, breached_trackers JSON, peak_pct, total_excess_cost_usd.
  - **Supersedes H3** — no per-hour throttle, incident state machine instead.

**Attack 9 — Noise — Multi-project admin overload.** Accepted at current scale (small admin teams, few projects). Revisit if Semantaix scales to multi-tenant SaaS.

**Attack 10 — REAL — RAG reindex / moderation blast.**
- *Threat:* Moderator approving batches of KB candidates triggers many LLM `client_materials_analyzer` calls; dashboard shows "spike" with no customer-message correlation.
- *Mitigation locked:*
  1. **Hard cap: 20 active client materials per project.** Cap enforced in Epic 12 (table owner). KB upload exceeding cap → auto-analysis still runs but material is not registered; operator gets "material slots full (20/20)" ack.
  2. **Epic 14 contributes** a `moderation_triggered` value to the `call_outcome` enum, so dashboard distinguishes moderator-driven from customer-driven cost.
  3. **Action item for `bmad-correct-course`:** flag the 20-material cap as amendment to Epic 12 PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) — story 12-05b scope expansion.

#### Round 3 — 4 attacks

**Attack 11 — Noise — Privacy boundary from 30d raw rows.** Token-count + timestamp metadata + transcript DB correlation accepted as low-concern at current scale and operator trust model.

**Attack 12 — REAL — UTC day vs admin local day.**
- *Threat:* I1 stores days in UTC; Moscow admins (UTC+3) see "today" cut off at 03:00 MSK, splitting evening traffic across two day columns.
- *Mitigation locked:* **UI renders days in admin's browser timezone**; storage stays UTC. JS converts on read. No schema change. Dashboard time-selector labels (`1d`, `1w`, `1m`) computed against browser-local "today".

**Attack 13 — Noise — USD-only displayed to RUB-paying clients.** Accepted; admins sophisticated enough to mentally convert. No multi-currency display in scope.

**Attack 14 — REAL — SQLite WAL contention.**
- *Threat:* High-frequency async writes + dashboard chart queries hitting 30d × N-projects raw rows can stall writers.
- *Mitigation locked:* **Summary-table-first reads in dashboard.** All chart/tile queries hit `usage_daily_summary` only. Raw rows queried *only* on drill-down (small, bounded result set per click). Separates the hot write path from the heavy read path.

---

## Phase 3 — Consolidated Decision Shortlist & Risk Map

_Solution Matrix skipped; user's decisive answers collapsed the trade-off surface during Phases 1-2._

### Locked Design Decisions (ready for `bmad-create-epics-and-stories`)

**Architecture**
- Epic 14 ships **three separable trackers** in one `semantaix_usage.db` (WAL mode):
  1. **OpenRouter LLM tracker** — money-bearing, per-call rows + per-(project/day/model) summaries.
  2. **Message-volume tracker** — per-event rows, no LLM tie-in.
  3. **HITL event tracker** — counts ticket lifecycle events (created/assigned/replied/resolved).
- Attribution unit = **project** for all three trackers.
- Single ingestion seam in api with `tracker_type` discriminator; async fire-and-forget queue (K1, K2).
- Instrumentation **scattered** across call sites (A1) — each answerer reports its own.

**Schema (5 tables in `semantaix_usage.db`)**
- `usage_llm_calls` — raw, 30d retention. Columns include `project_id, model_name, prompt_tokens, completion_tokens, cost_usd (NULL-tolerant), call_outcome enum, created_at`.
- `usage_messages` — raw, 30d. Columns: `project_id, direction (in/out), participant_role (customer/operator), created_at`.
- `usage_hitl_events` — raw, 30d. Columns: `project_id, event_type, ticket_id, created_at`.
- `usage_daily_summary` — forever, primary key `(project_id, day_utc, tracker_type, model_name NULL for non-LLM)`. Includes `wasted_cost_usd` for LLM tracker.
- `usage_incidents` — incident state machine (started_at, ended_at, breached_trackers JSON, peak_pct, total_excess_cost_usd).

**`call_outcome` enum values**
- `customer_visible_answer` — successful answer reached customer.
- `verifier_rejected` — grounded LLM passed but verifier blocked → escalated.
- `escalated_to_hitl` — no answerer handled or verifier rejected → HITL ack + ticket.
- `guardrails_blocked` — final regex/profanity check blocked the output.
- `moderation_triggered` — call originated from a moderator action (KB upload analysis), not customer traffic.
- `error` — LLM call errored (out of scope for Epic 14 — separate error monitoring).

**Pricing & data source**
- Token + cost from OpenRouter `usage` response fields. No self-counting.
- Missing `cost` → record token counts, leave `cost_usd` NULL.
- USD only. Model name per row.

**Storage retention**
- Raw rows: **30 days** rolling.
- Daily summaries: **forever**, per `(project, day, model, tracker)`.
- Hourly rollups: **dropped** — raw 30d covers spike-debugging window.

**RBAC**
- Money filter enforced at **API layer** — operator endpoints never return cost fields.
- Operators see token + volume counts across all three trackers (no cost).
- Operator ↔ exactly one project (refined model: many flat-list operators per project; one project per operator).

**Web UI**
- Single dashboard page (not tabs).
- Time selector: 1d / 1w / 1m / custom range. Days rendered in admin's browser timezone.
- Line/sparkline charts. Per-model breakdown.
- Drill-down to call list for last 30d. Reads summaries first; raw only on drill-down.
- "Wasted spend" tile with per-outcome breakdown.
- Empty project → "No data yet" placeholder.

**Bot command**
- One role-aware `/usage` — all three trackers in one text reply + web UI deep link.
- Project-scoped to operator's one project (admin sees the project addressed in chat, or specify by name).
- No chart images, text only.

**Alerting (triple-indicator + incident grouping)**
- **Indicator 1 — Daily budget cap** (admin-set per project at creation, required field). Alert at 80% spend, escalate at 100%. Always active including day 1.
- **Indicator 2 — Per-message cost outlier**: any single inbound message > $1.00 fires. Always active.
- **Indicator 3 — Rolling-avg rule** (active day 8+):
  - LLM cost: > 200% of 7-day rolling avg AND > $3 absolute increase.
  - Messages: > 200% AND > +50 absolute increase.
  - HITL: > 200% AND > +20 absolute increase.
  - All thresholds stored in `hitl_runtime_config` (runtime-configurable per project).
- **Channel**: admin Telegram DM only. No web UI banner. No kill-switch.
- **Incident grouping** (supersedes H3 throttle):
  - `INCIDENT START` — first threshold cross while no active incident for that project.
  - `INCIDENT EXPAND` — additional tracker breaches during active incident.
  - `INCIDENT END` — all breached trackers below threshold for ≥60 min continuous. Message includes duration, peak %, total excess cost.
- **Hysteresis**: 60-min continuous under-threshold required to close.

**Timezone & windows**
- Storage: UTC.
- Display: admin's browser timezone.
- Week starts Monday. "Last 30 days" = rolling.

**Performance & failure modes**
- SQLite is fine; revisit if a single project crosses ~100k LLM calls/day.
- Indexes: `(project_id, created_at)` raw, `(project_id, day_utc, model_name)` daily, `(project_id, started_at)` incidents.
- Async queue, fire-and-forget. Write failure = silent data loss; LLM call unaffected.
- Summary-table-first reads in dashboard; raw only on drill-down.

**Backup / corruption**
- `semantaix_usage.db` covered by Epic 7 backup runbook (extend it).
- On corruption: degrade gracefully — LLM calls continue, dashboard shows "Usage data unavailable".

**Rollout**
- Fresh start — no backfill.
- Empty-state placeholder.
- Always-on, no feature flag.

### Edge-Case / Risk Map (6 confirmed + accepted limitations)

| # | Risk | Mitigation (locked) |
|---|------|---------------------|
| 1 | Verifier / multi-call answerer "wasted spend" invisible | `call_outcome` enum + `wasted_cost_usd` summary column + Wasted-Spend tile in UI |
| 5 | Cold-start false alarms (rolling avg biased low / no signal in week 1) | Triple-indicator alerting (budget cap + per-message outlier always-on; rolling avg day 8+) |
| 8 | Cascading alert storm during multi-tracker breaches | Incident grouping state machine (START / EXPAND / END), supersedes per-hour throttle |
| 10 | RAG/moderation LLM blast misattributed as customer spike | 20-material cap (Epic 12 amendment) + `moderation_triggered` outcome value |
| 12 | UTC day vs admin local-day skews charts | Browser-tz rendering of days; UTC storage |
| 14 | SQLite WAL contention from 30d raw + heavy chart queries | Summary-first dashboard reads; raw only on drill-down |

**Accepted limitations (no mitigation in scope):**
- Async write loss on restart silently drops in-flight rows (K2).
- Operator can side-channel-infer cost from token counts + known model rates (G2).
- Multi-project admin alert overload (Attack 9) — revisit at SaaS scale.
- Cost-stuffing by malicious operator (Attack 6) — daily budget cap is the only defense.
- USD-only display (D1) — no multi-currency conversion.
- OpenRouter schema drift (Attack 7) — future `usage` fields require deliberate schema bump.
- Privacy: 30d token-count metadata correlatable with transcript DB (Attack 11) — accepted.

### Cross-Epic Action Items (for `bmad-correct-course`)

These are **dependencies Epic 14 needs but does not own**. Flag both before opening Epic 14's `bmad-create-epics-and-stories`:

1. **Epic 10 amendment** — remove `hitl_primary_operator_username` runtime config. Bot gateway operator-routing logic to be rewritten for flat-list-many-operators-per-project model. Operator-to-project mapping enforced as 1-to-1 (operator side).
2. **Epic 12 amendment to PR [#82](https://github.com/flexsent-labs/semantaix/pull/82)** — add 20-material cap to story 12-05b (KB-upload → auto-analysis). KB upload exceeding cap → analysis runs, material not registered, operator ack shows "material slots full (20/20)".

### Proposed Story Split for Epic 14 (informal — `bmad-create-epics-and-stories` will refine)

- **14-01** — Schema + base tables in `semantaix_usage.db` (foundation; blocks all).
- **14-02** — OpenRouter LLM-call instrumentation (scattered call sites + async queue + `call_outcome` enum).
- **14-03** — Message-volume instrumentation (bot_gateway emits in/out events).
- **14-04** — HITL event instrumentation (api emits ticket lifecycle events).
- **14-05** — Daily roll-up worker + 30d raw retention purge (scheduler service).
- **14-06** — Web UI dashboard (charts, drill-down, wasted-spend tile, browser-tz rendering).
- **14-07** — API endpoints + RBAC (money-stripped operator endpoints, project scoping).
- **14-08** — Bot `/usage` command (role-aware, three-tracker output, deep link).
- **14-09** — Alerting: budget cap config + per-message outlier + rolling avg + incident state machine.
- **14-10** — Epic signoff: backup runbook update, runtime-config UI, e2e tests.

Dependency hints: 14-01 blocks all. 14-02/14-03/14-04 parallel after 14-01. 14-05 after 14-02/14-03/14-04. 14-06/14-07/14-08 parallel after 14-05. 14-09 after 14-05. 14-10 final.






