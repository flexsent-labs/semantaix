---
stepsCompleted: [1, 2, 3, 4]
inputDocuments:
  - _bmad-output/planning-artifacts/PRD.md
  - _bmad-output/planning-artifacts/architecture.md
  - _bmad-output/brainstorming/brainstorming-session-2026-05-26-1500.md
  - _bmad-output/planning-artifacts/epics/README.md
  - _bmad-output/planning-artifacts/epics/epic-01-telegram-llm-suggestions.md
  - _bmad-output/planning-artifacts/epics/epic-02-incident-alert-foundation.md
  - _bmad-output/planning-artifacts/epics/epic-03-guardrails-validity.md
  - _bmad-output/planning-artifacts/epics/epic-04-hitl-escalation.md
  - _bmad-output/planning-artifacts/epics/epic-05-rag-foundation.md
  - _bmad-output/planning-artifacts/epics/epic-06-knowledge-moderation.md
  - _bmad-output/planning-artifacts/epics/epic-07-backup-restore-hardening.md
  - _bmad-output/planning-artifacts/epics/epic-08-tenant-knowledge-ops-and-answer-traces.md
  - _bmad-output/planning-artifacts/epics/epic-09-operator-kb-growth.md
  - _bmad-output/planning-artifacts/epics/epic-10-multi-operator-projects.md
  - _bmad-output/planning-artifacts/epics/epic-10-5-operator-project-model-refinement.md
  - _bmad-output/planning-artifacts/epics/epic-11-calendar-availability-scheduling.md
  - _bmad-output/planning-artifacts/epics/epic-12-sales-conversation-persona.md
  - _bmad-output/planning-artifacts/epics/epic-13-unified-project-services-catalog.md
  - docs/project-context.md
scope: 'Epic 14 (Usage / Token / Cost Monitoring) — new epic; epics 01-13 already shipped or in progress'
output_layout: 'one-file-per-epic (epic-14-usage-cost-monitoring.md) + stories/epic-14/ subdirectory'
---

# Semantaix - Epic Breakdown

## Overview

This document is the comprehensive epic-and-story breakdown for **Semantaix**, decomposing the PRD requirements and Architecture decisions into implementable stories.

Semantaix is a brownfield project: **Epics 01–13** are documented as individual files in `epics/epic-NN-*.md` and have either shipped or are in flight. This document is being (re-)materialized to add **Epic 14 — Usage / Token / Cost Monitoring + Cost-Spike Alerting** while preserving the existing one-file-per-epic layout. The Epic List below is the index; Epic 14 is the focus of the current `bmad-create-epics-and-stories` run.

## Requirements Inventory

### Functional Requirements

Sourced from `_bmad-output/planning-artifacts/PRD.md` (FR-1 — FR-25, canonical) plus a proposed Epic-14 addendum **FR-26 — FR-36** derived from the 2026-05-26 brainstorming session. The PRD must be amended with FR-26–FR-36 as a Step-2 prerequisite (or in a parallel PRD-amend PR); they are listed here so the Epic-14 stories can be authored against a stable requirements surface.

**Shipped / in-flight (PRD canonical):**

- **FR-1 Telegram Conversation Flow** — bot receives via webhook, loads context, attempts AI answer; latency-bounded; full response persisted with role + trace metadata.
- **FR-2 RAG Retrieval and Answering** — lemma-overlap retrieval over `rag_chunks`; grounded responses; guardrail fallback when grounding/confidence below threshold.
- **FR-3 Human-in-the-Loop Escalation** — durable ticket on AI fail; lifecycle `open → assigned → resolved` (operator reply auto-resolves); operator metadata never leaks to end user.
- **FR-4 Configurable HITL Recipient** — Web UI Settings + `/hitl_config @username <chat_id>`; admin-gated by `HITL_CONFIG_ADMIN_USERNAME`; persists without restart.
- **FR-5 Full Transcript Storage + Knowledge Candidate Extraction** — every message in `semantaix_story1.db`; noise-filtered candidates feed moderation; only approved candidates index into RAG.
- **FR-6 Knowledge Moderation Workflow** — review/edit/approve/reject; approval triggers re-index; every action audit-logged.
- **FR-7 Alerts and Incident Management UI** — Web UI Alerts tab with read/unread, filters, ack/resolve, event timeline; transitions persist.
- **FR-8 Critical Telegram Incident Notifications** — fingerprint-deduped critical incidents notify on-call `@ajdevy`; delivery recorded in `incident_events`.
- **FR-9 Health Endpoints** — `/health/live | /ready | /startup` per service; readiness reflects dependency checks.
- **FR-10 Structured Logging and Trace Correlation** — JSON logs with `trace_id, conversation_id, escalation_ticket_id, incident_id` correlating across services.
- **FR-11 Resilience for External Providers** — retry+backoff+jitter, rate-limit handling, circuit breaker, HITL fallback on degraded mode.
- **FR-12 Docker-First Runtime and Deployment** — containerized services; compose stack supports local/dev/prod parity; health checks declared.
- **FR-13 Answer Guardrail Decision Engine** — explicit validity checks (retrieval sufficiency, grounding, confidence, safety); failed answers escalate to HITL with logged reason.
- **FR-14 Backup and Restore Operations** — tar.gz backups of SQLite stores; UI shows last backup + storage location; token-confirmed restore; `restore_completed/failed` audit events.
- **FR-15 Tenant-Scoped Answer Transparency ("Why This Answer")** — durable append-only `answer_traces`; read-only "Why this answer" UI panel; missing trace raises incident.
- **FR-16 Natural-Language Tenant Knowledge Operations** — bot-first conversational CRUD on tenant knowledge with preview, confirm, versioning, reindex, audit; optional moderation-candidate routing.
- **FR-17 Trace-Originated Knowledge Correction Loop** — guided correction from a trace; updates future retrieval without rewriting past traces; failures emit incidents.
- **FR-18 Operator Google Calendar Connect (OAuth)** *(Epic 11)* — `/connect_calendar` slash, single-use server-stored `state` token, `calendar.readonly` scope, encrypted refresh token; reconnect detection + incident + token clear on refresh failure.
- **FR-19 Calendar Availability Answering** *(Epic 11)* — one live `freeBusy` per question, intersected with per-service rules; project tz; uncertainty escalates to calendar operator.
- **FR-20 Per-Service Scheduling Rules** *(Epic 11)* — duration, working-hours windows (multi-window/day), service-days, date exceptions; runtime-editable.
- **FR-21 Per-Project Opt-In Gating** *(Epic 11)* — default-off tri-state (not enabled / connected but reconnect-needed / connected); enable === connect; disable kept; disconnect operator-only.
- **FR-22 Service Resolution from Russian Text** *(Epic 11)* — lemma-match against calendar-eligible subset of `project_services`; one clarifying turn before escalation; never guesses.
- **FR-23 Canonical `project_services` table** *(Epic 13)* — rename `calendar_service_rules` → `project_services` + catalog columns; idempotent migration; fresh-deploy path; `UNIQUE(project_id, lower(name))`; calendar-eligible iff `duration_minutes IS NOT NULL`.
- **FR-24 Operator-facing service editing surface (slash + NL)** *(Epic 13)* — `/service add|edit|remove|list` + Russian NL dialog (`добавь услугу …`); regex extraction; preview/confirm/cancel; operator-only remove; admin must be registered operator for shared ops.
- **FR-25 Catalog answer reads structured services first (humanistic, question-tailored)** *(Epic 13)* — natural Russian prose at repo boundary (no field labels); merge with `_catalog_digest` via lemma dedup; question-tailored prompting; trace `source_id` ∈ {project_services, catalog_digest, merged}.

**Epic 14 addendum (proposed — derived from brainstorming session 2026-05-26-1500.md; pending PRD amendment):**

- **FR-26 Three-Tracker Usage Architecture** — Three separable, project-scoped trackers — OpenRouter LLM (money-bearing), message volume, HITL events — share a single SQLite store `semantaix_usage.db` (WAL) with separate tables and per-tracker daily roll-ups. Attribution unit = **project** for all three.
- **FR-27 Polymorphic Ingestion Seam** — A single api-level ingestion seam accepts `(tracker_type, project_id, payload)` and dispatches to the correct raw-event table. Writes are **async fire-and-forget** from instrumentation sites; LLM/message/HITL operations never block or fail on usage-write failure (silent loss is the acceptable degradation per K2).
- **FR-28 OpenRouter LLM Usage Capture** — Every OpenRouter call records `project_id, model_name, prompt_tokens, completion_tokens, cost_usd (NULL-tolerant), call_outcome, created_at` from the OpenRouter `usage` response fields. No self-counting. The `call_outcome` enum is `customer_visible_answer | verifier_rejected | escalated_to_hitl | guardrails_blocked | moderation_triggered | error`. Instrumentation is scattered across answerer/call-site code (A1).
- **FR-29 Message-Volume Capture** — `bot_gateway` emits one event per inbound/outbound message with `project_id, direction, participant_role, created_at`. Zero-LLM messages still count toward the message tracker but never toward the LLM tracker (separates volume from spend).
- **FR-30 HITL Event Capture** — api emits one event per HITL ticket lifecycle transition (`created | assigned | replied | resolved`) with `project_id, event_type, ticket_id, created_at`. Counts events, not messages.
- **FR-31 Daily Roll-up + 30-day Raw Retention** — A scheduler-service job rolls raw rows into `usage_daily_summary` keyed `(project_id, day_utc, tracker_type, model_name NULL for non-LLM)` including LLM-tracker-only `wasted_cost_usd`. Raw rows retained **30 days** rolling; daily summaries retained **forever**. Hourly rollups intentionally dropped.
- **FR-32 Usage Dashboard (Web UI)** — Single project-scoped dashboard page (not three tabs); 1d / 1w / 1m / custom-range selector; line/sparkline charts; per-model breakdown; drill-down to last-30d call list; "Wasted spend" tile with per-`call_outcome` breakdown; "No data yet" empty state; days rendered in **admin browser timezone** (storage stays UTC).
- **FR-33 Usage API + RBAC** — New api endpoints power the dashboard and `/usage` bot command. The **money filter is enforced at the API layer**: operator endpoints never return `cost_usd` or any derivative field. Project scoping is enforced by the operator-↔-project mapping (Epic 10.5 refinement: many flat-list operators per project, one project per operator). Reads prefer `usage_daily_summary` ("summary-first") and hit raw rows only on explicit drill-down requests (mitigates Attack 14, WAL contention).
- **FR-34 Role-Aware `/usage` Bot Command** — One slash command `/usage` returns all three trackers in one text message + a deep link to the Web UI dashboard. **Admin output** includes cost (LLM block) + volume (messages, HITL); **operator output** strips cost and shows token + message + HITL counts only. Text-only — no chart images. Project resolved from the operator's single project assignment.
- **FR-35 Cost-Spike Alerting (Triple-Indicator)** — Three alert indicators per project:
  1. **Daily budget cap** — admin-set per project at creation (required field); fires at **80% spend** (warning) and **100%** (breach). Always active including day 1.
  2. **Per-message cost outlier** — any single inbound message > **$1.00** fires. Always active.
  3. **Rolling-avg rule** — active **day 8+**:
     - LLM cost: > 200% of 7-day rolling avg AND > $3 absolute increase
     - Messages: > 200% AND > +50 absolute increase
     - HITL: > 200% AND > +20 absolute increase
  All thresholds are stored in `hitl_runtime_config` (runtime-configurable per project). Channel: admin Telegram DM only. No web UI banner; no kill-switch.
- **FR-36 Incident Grouping (Usage)** — A state machine in a new `usage_incidents` table replaces per-hour throttling. Events: **INCIDENT_START** (first threshold cross while no active incident for that project) → **INCIDENT_EXPAND** (additional tracker breaches during active incident) → **INCIDENT_END** (all breached trackers under threshold for ≥**60 min continuous**; message includes duration, peak %, total excess cost). 60-min continuous under-threshold = hysteresis to close. Storage: `project_id, started_at, ended_at, breached_trackers JSON, peak_pct, total_excess_cost_usd`.

### NonFunctional Requirements

From PRD §5 (canonical) + Epic 14 addenda (proposed):

**Shipped / in-flight (PRD canonical):**

- **NFR-1 Reliability** — Graceful degradation during provider/dependency incidents.
- **NFR-2 Observability** — Operational metrics + structured logs for debugging and incident triage.
- **NFR-3 Security** — Secrets env-managed (never committed); admin actions auditable. OAuth client secret + token encryption key in env; per-operator refresh tokens encrypted-at-rest in SQLite; calendar consent read-only-scoped; tokens never logged.
- **NFR-4 Performance** — MVP response latency target + throughput thresholds defined and validated.
- **NFR-5 Maintainability** — Explicit service boundaries + interfaces.
- **NFR-6 Deployability** — DigitalOcean deployment path documented + reproducible under Docker-first model.
- **NFR-7 Recoverability** — Routine backup + controlled restore with defined RPO/RTO targets on retrieval store.

**Epic 14 addendum (proposed):**

- **NFR-8 Usage-Capture Liveness** — Usage-capture failure must never observably degrade the user-facing pipeline. LLM calls, message routing, and HITL ticket transitions complete on usage-write timeout/failure; failures are logged but do not raise (`async fire-and-forget`, per FR-27 / brainstorm K2).
- **NFR-9 Usage RBAC** — The cost/money axis is enforceable at the API boundary: a request lacking admin scope receives no `cost_usd` or any derived monetary field (dashboard chart payloads, `/usage` bot output, drill-down detail rows). Operator-scope responses are byte-clean of monetary content (no leakage via stringified totals or summary rows). Server-side enforcement; client-side filtering not acceptable.
- **NFR-10 Usage Storage Scale** — SQLite is the system of record for `semantaix_usage.db`. The design must remain correct up to ~100k LLM calls / day / project; a single project crossing that threshold is a documented "revisit storage choice" trigger, not a runtime failure (per brainstorm K3).
- **NFR-11 Usage Backup Coverage** — `semantaix_usage.db` is included in the Epic 7 backup runbook (tar.gz archive of `.data/*.db`). On corruption: graceful degradation — LLM calls continue, dashboard shows "Usage data unavailable" banner instead of crashing.

### Additional Requirements

Sourced from `_bmad-output/planning-artifacts/architecture.md` (as-built) and `docs/project-context.md`. These are technical/implementation constraints that bind all stories in this epic.

**Stack / Conventions (must follow on Epic 14 code):**

- Python 3.11, FastAPI 0.115.12, Pydantic Settings 2.9.1, httpx 0.28.1, ruff line-length 100, pytest 8.3.5 + `pytest-asyncio` (function-scoped loop).
- `pytest-cov 5.0.0` with `fail_under = 100` on `platform_common/` + `services/` — Epic 14 modules MUST ship at 100% line coverage.
- `from __future__ import annotations` atop every module; PEP 604 unions; keyword-only public methods.
- Interfaces are `typing.Protocol` with constructor-injected dependencies; value objects are `@dataclass(frozen=True)`.
- **Network I/O async, SQLite sync via `asyncio.to_thread`** — no `sqlite3` wrapped in `async def`; all DB access funneled through `*Repository` classes; no raw SQL outside repos.
- **Apps via `platform_common/app_factory.py`** — new services/routers extend it; never hand-roll health endpoints. Service-to-service auth via `internal_service_token` (Bearer).
- **Settings centralized** — new env vars get a `.env.example` entry AND a `Settings` field; never scatter `os.getenv`.
- **Time is injected, never ambient** — accept `now` / a clock at the seam; UTC ISO-8601 at rest; tz-aware datetimes throughout.
- **Structured logging** — event name `snake_case verb_noun` literal; always include `trace_id`; never f-string the message; never log secrets.
- **AnswerPipeline ordering is routing** — Epic 14 instrumentation must be additive (scattered call-site reports); MUST NOT alter pipeline order or short-circuit logic.

**Carry-forward constraint (binding for Epic 14 per `epics/README.md`):**

- **From Epic 03 onward, every epic must integrate with the incident/alerts solution from Epic 02.** Epic 14 alerting (FR-35/FR-36) emits incidents into the Epic 02 `incidents` engine (not bespoke channels) — `INCIDENT_START` / `INCIDENT_EXPAND` / `INCIDENT_END` events of the Usage incident state machine ARE first-class incidents with appropriate `severity` and `fingerprint`.

**Data store conventions (Epic 14 specific):**

- New SQLite store `semantaix_usage.db` (WAL mode) follows the one-file-per-concern pattern. Owned by api; web_ui reads it RO; scheduler runs the roll-up + retention purge against it. Tables:
  - `usage_llm_calls` — raw, 30d retention. Columns include `project_id, model_name, prompt_tokens, completion_tokens, cost_usd (NULL-tolerant), call_outcome, created_at`.
  - `usage_messages` — raw, 30d. Columns: `project_id, direction (in/out), participant_role (customer/operator), created_at`.
  - `usage_hitl_events` — raw, 30d. Columns: `project_id, event_type, ticket_id, created_at`.
  - `usage_daily_summary` — forever. PK `(project_id, day_utc, tracker_type, model_name NULL for non-LLM)`. Includes `wasted_cost_usd` for LLM tracker.
  - `usage_incidents` — state machine. Columns: `project_id, started_at, ended_at, breached_trackers JSON, peak_pct, total_excess_cost_usd`.
- Indexes: `(project_id, created_at)` on raw tables; `(project_id, day_utc, model_name)` on summary; `(project_id, started_at)` on incidents.
- Money axis enforced at the API layer — operator endpoints physically exclude `cost_usd` from SQL projections (`SELECT` list), not just suppress in serialization.

**Cross-epic action items already captured (Epic 14 depends on these):**

- **Epic 10.5 — Operator/Project Model Refinement** (backlog file `epic-10-5-operator-project-model-refinement.md`) MUST land before FR-33 RBAC and the `/usage` command (FR-34) — removes `hitl_primary_operator_username` and establishes flat-list-many-operators-per-project (one project per operator).
- **Epic 12 — 20-Material Cap** (story 12-05b, already merged in PR #83) ensures moderator KB-upload approval blasts cannot spike LLM usage beyond a known ceiling (mitigates Attack 10). Epic 14 reads `call_outcome = moderation_triggered` to attribute these correctly.

**Rollout (Epic 14):**

- Fresh start — **no backfill** of historical LLM calls. Empty project shows "No data yet" placeholder.
- **Always-on** — no feature flag (J3).
- Daily budget cap is a required field at project creation (introduced by FR-35); existing projects need a one-time backfill of this field as part of Epic 14 release rollout.

### UX Design Requirements

No dedicated UX Design specification document exists for this project. Epic 14's Web UI work (dashboard, drill-down, "Wasted spend" tile, time-range selector) is treated as inline UX requirements bundled into FR-32 and the relevant Epic 14 stories. Acceptance criteria for UI behavior will be authored in Step 2 / Step 3 (Story Acceptance Criteria) of this workflow.

### FR Coverage Map

| Requirement | Mapped Epic(s) |
|---|---|
| FR-1 Telegram Conversation Flow | Epic 01 |
| FR-2 RAG Retrieval and Answering | Epic 05 (+ Epic 01) |
| FR-3 HITL Escalation | Epic 04 |
| FR-4 Configurable HITL Recipient | Epic 04 |
| FR-5 Transcript Storage + Candidate Extraction | Epic 01 + Epic 06 |
| FR-6 Knowledge Moderation | Epic 06 |
| FR-7 Alerts/Incidents UI | Epic 02 |
| FR-8 Critical Telegram Notifications | Epic 02 |
| FR-9 Health Endpoints | Epic 01 / cross-cutting |
| FR-10 Structured Logging + Trace | Epic 01 / cross-cutting |
| FR-11 Provider Resilience | Epic 02 + Epic 03 |
| FR-12 Docker-First Runtime | Epic 01 / cross-cutting |
| FR-13 Guardrail Decision Engine | Epic 03 |
| FR-14 Backup/Restore | Epic 07 |
| FR-15 Tenant Answer Transparency | Epic 08 |
| FR-16 NL Tenant Knowledge Ops | Epic 08 |
| FR-17 Trace-Originated Correction Loop | Epic 08 |
| FR-18 Calendar OAuth Connect | Epic 11 |
| FR-19 Calendar Availability Answering | Epic 11 |
| FR-20 Per-Service Scheduling Rules | Epic 11 → Epic 13 (table now `project_services`) |
| FR-21 Per-Project Opt-In Gating | Epic 11 |
| FR-22 Service Resolution from Russian Text | Epic 11 / Epic 13 |
| FR-23 Canonical `project_services` Table | Epic 13 |
| FR-24 Operator Service Editing Surface | Epic 13 |
| FR-25 Catalog Answer Reads Structured First | Epic 13 |
| **FR-26 Three-Tracker Usage Architecture** | **Epic 14 → Story 14.01** |
| **FR-27 Polymorphic Ingestion Seam** | **Epic 14 → Story 14.02** |
| **FR-28 LLM Usage Capture** | **Epic 14 → Story 14.02** |
| **FR-29 Message-Volume Capture** | **Epic 14 → Story 14.03** |
| **FR-30 HITL Event Capture** | **Epic 14 → Story 14.04** |
| **FR-31 Daily Roll-up + 30d Retention** | **Epic 14 → Story 14.05** |
| **FR-32 Usage Dashboard (Web UI)** | **Epic 14 → Story 14.06** |
| **FR-33 Usage API + RBAC** | **Epic 14 → Story 14.07** |
| **FR-34 `/usage` Bot Command** | **Epic 14 → Story 14.08** |
| **FR-35 Cost-Spike Alerting (Triple-Indicator)** | **Epic 14 → Story 14.09** |
| **FR-36 Incident Grouping (Usage)** | **Epic 14 → Story 14.09** |
| NFR-1 Reliability | cross-cutting (all epics) |
| NFR-2 Observability | cross-cutting (esp. Epic 02) |
| NFR-3 Security | cross-cutting (esp. Epic 11) |
| NFR-4 Performance | cross-cutting |
| NFR-5 Maintainability | cross-cutting |
| NFR-6 Deployability | cross-cutting (esp. Epic 01) |
| NFR-7 Recoverability | Epic 07 |
| **NFR-8 Usage-Capture Liveness** | **Epic 14 (cross-cutting)** |
| **NFR-9 Usage RBAC** | **Epic 14 → Story 14.07** |
| **NFR-10 Usage Storage Scale** | **Epic 14 → Story 14.01 + 14.05** |
| **NFR-11 Usage Backup Coverage** | **Epic 14 → Story 14.10** |

## Epic List

Feature-sequential — only one feature epic in implementation at a time, per `epics/README.md` hard rule.

| # | Epic | Status | File |
|---|---|---|---|
| 01 | Telegram + LLM Suggestions | Shipped | [epic-01-telegram-llm-suggestions.md](epics/epic-01-telegram-llm-suggestions.md) |
| 02 | Incident & Alert Foundation | Shipped | [epic-02-incident-alert-foundation.md](epics/epic-02-incident-alert-foundation.md) |
| 03 | Guardrails & Validity | Shipped | [epic-03-guardrails-validity.md](epics/epic-03-guardrails-validity.md) |
| 04 | HITL Escalation | Shipped | [epic-04-hitl-escalation.md](epics/epic-04-hitl-escalation.md) |
| 05 | RAG Foundation | Shipped | [epic-05-rag-foundation.md](epics/epic-05-rag-foundation.md) |
| 06 | Knowledge Moderation | Shipped | [epic-06-knowledge-moderation.md](epics/epic-06-knowledge-moderation.md) |
| 07 | Backup / Restore Hardening | Shipped | [epic-07-backup-restore-hardening.md](epics/epic-07-backup-restore-hardening.md) |
| 08 | Tenant Knowledge Ops + Answer Traces | Shipped | [epic-08-tenant-knowledge-ops-and-answer-traces.md](epics/epic-08-tenant-knowledge-ops-and-answer-traces.md) |
| 09 | Operator KB Growth | Shipped | [epic-09-operator-kb-growth.md](epics/epic-09-operator-kb-growth.md) |
| 10 | Multi-Operator Projects | Shipped | [epic-10-multi-operator-projects.md](epics/epic-10-multi-operator-projects.md) |
| 10.5 | Operator/Project Model Refinement | Backlog (Epic 14 prerequisite) | [epic-10-5-operator-project-model-refinement.md](epics/epic-10-5-operator-project-model-refinement.md) |
| 11 | Calendar Availability & Scheduling | Shipped | [epic-11-calendar-availability-scheduling.md](epics/epic-11-calendar-availability-scheduling.md) |
| 12 | Sales Conversation Persona | In progress (planning merged via PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) + cap PR [#83](https://github.com/flexsent-labs/semantaix/pull/83); stories backlog) | [epic-12-sales-conversation-persona.md](epics/epic-12-sales-conversation-persona.md) |
| 13 | Unified Project Services Catalog | Shipped (PR [#80](https://github.com/flexsent-labs/semantaix/pull/80)) | [epic-13-unified-project-services-catalog.md](epics/epic-13-unified-project-services-catalog.md) |
| **14** | **Usage / Token / Cost Monitoring + Cost-Spike Alerting** | **Planning (Step 2 of `bmad-create-epics-and-stories` complete; Step 3 next)** | [epic-14-usage-cost-monitoring.md](epics/epic-14-usage-cost-monitoring.md) |

Detailed Epic 14 design — goal, in-scope, out-of-scope, dependencies, exit criteria, and the 10-story split — is authored in [epic-14-usage-cost-monitoring.md](epics/epic-14-usage-cost-monitoring.md). This index file remains canonical for the Requirements Inventory (FR-1 — FR-36 / NFR-1 — NFR-11) and Epic List.

---

## Epic 14: Usage / Token / Cost Monitoring + Cost-Spike Alerting

Surface LLM token usage, message volume, and HITL activity per project so admins can see spend over 1d/1w/1m windows in the Web UI and via a bot command, while operators see token/message/HITL volume only (no money) on the same surfaces. Implements **FR-26 — FR-36** and **NFR-8 — NFR-11**. Detailed scope, dependencies, and exit criteria in [epic-14-usage-cost-monitoring.md](epics/epic-14-usage-cost-monitoring.md). Story files live in [epics/stories/epic-14/](epics/stories/epic-14/).

### Story 14.01: `semantaix_usage.db` schema, idempotent migration, repository skeletons

As a **platform engineer**,
I want a dedicated, well-indexed, WAL-mode SQLite store for usage telemetry separated from the rest of the persistence layer,
So that scattered instrumentation in later stories can write fire-and-forget without contention against business-critical DBs.

**Acceptance Criteria:**

**Given** a fresh `.data/` directory
**When** the api boots
**Then** `.data/semantaix_usage.db` is created with WAL mode, all five tables (`usage_llm_calls`, `usage_messages`, `usage_hitl_events`, `usage_daily_summary`, `usage_incidents`) and required indexes
**And** running the bootstrap a second time produces zero schema changes (idempotent).

Full ACs and test plan: [story-14-01-usage-db-schema-and-migration.md](epics/stories/epic-14/story-14-01-usage-db-schema-and-migration.md).

### Story 14.02: OpenRouter LLM-call instrumentation + `UsageRecorder` seam + `call_outcome` enum

As an **admin**,
I want every OpenRouter LLM call attributed to a project with token counts, cost, model name, and a downstream outcome,
So that the dashboard, alerts, and `/usage` command can show me where my spend goes — including the wasted-spend slice that doesn't reach the customer.

**Acceptance Criteria:**

**Given** a customer message triggers an OpenRouter LLM call
**When** the response returns successfully
**Then** exactly one row appears in `usage_llm_calls` with non-NULL `prompt_tokens`/`completion_tokens`, populated `cost_usd` (or NULL if OpenRouter omits it), correct `model_name`, and `call_outcome` matching what the answerer reported
**And** the inbound critical path completes BEFORE the consumer task processes the queued item (fire-and-forget verified).

**Given** the usage DB is unwritable
**When** an LLM call completes
**Then** `usage_record_failed` is logged
**And** the LLM call's customer-facing answer is delivered unchanged (NFR-8).

Full ACs: [story-14-02-llm-instrumentation-and-recorder.md](epics/stories/epic-14/story-14-02-llm-instrumentation-and-recorder.md).

### Story 14.03: Message-volume instrumentation (bot_gateway)

As an **admin or operator**,
I want to see total customer / operator message volume per project per day independent of LLM activity,
So that I can spot traffic spikes that don't correlate with cost spikes.

**Acceptance Criteria:**

**Given** a customer sends an inbound Telegram message
**When** the bot_gateway processes it
**Then** one `usage_messages` row appears with `direction='in', participant_role='customer'`, the resolved `project_id`, and the originating `trace_id`.

**Given** the bot delivers a reply outbound
**When** Telegram returns 200 OK
**Then** one `usage_messages` row appears with `direction='out', participant_role='operator'`.

**Given** the api is unreachable
**When** the bot processes a customer message
**Then** the message is still delivered + persisted normally
**And** `usage_record_dispatch_failed` is logged (NFR-8).

Full ACs: [story-14-03-message-volume-instrumentation.md](epics/stories/epic-14/story-14-03-message-volume-instrumentation.md).

### Story 14.04: HITL event instrumentation (api)

As an **admin or operator**,
I want to see HITL ticket-lifecycle event counts per project per day,
So that I can spot operator load, escalation rate, and resolution turnaround independent of LLM cost.

**Acceptance Criteria:**

**Given** a customer message escalates to HITL
**When** a new ticket is created and assigned
**Then** `usage_hitl_events` rows appear in order: `event_type='created'`, then `event_type='assigned'`, each with the matching `ticket_id` and `project_id`.

**Given** an operator replies via `/hitl/tickets/{id}/reply`
**When** the reply auto-resolves the ticket per FR-3
**Then** TWO rows appear in order: `event_type='replied'`, then `event_type='resolved'`.

Full ACs: [story-14-04-hitl-event-instrumentation.md](epics/stories/epic-14/story-14-04-hitl-event-instrumentation.md).

### Story 14.05: Daily roll-up worker + 30-day raw retention purge (scheduler)

As an **admin**,
I want the dashboard, alerts, and `/usage` command to be fast and accurate by reading pre-aggregated daily summaries,
So that WAL contention stays low and queries respond in milliseconds.

**Acceptance Criteria:**

**Given** raw rows exist for project 1 on `day_utc=2026-05-25` across all three trackers
**When** the daily roll-up runs after UTC midnight + offset
**Then** `usage_daily_summary` contains rows for `(1, '2026-05-25', 'llm', <model>)` per model, `(1, '2026-05-25', 'messages', '')`, and `(1, '2026-05-25', 'hitl', '')`
**And** `wasted_cost_usd` on each LLM row equals the sum over rows where `call_outcome ∈ {verifier_rejected, guardrails_blocked, error}`.

**Given** the scheduler runs the roll-up twice for the same day
**When** the second run completes
**Then** the summary table is byte-identical to after the first run (idempotent).

**Given** raw rows exist older than 30 days
**When** retention purge runs
**Then** older rows are deleted in bounded batches (10000 at a time)
**And** daily summaries for those days remain.

Full ACs: [story-14-05-daily-rollup-and-retention.md](epics/stories/epic-14/story-14-05-daily-rollup-and-retention.md).

### Story 14.06: Web UI usage dashboard (charts, drill-down, Wasted-spend tile, browser-tz rendering)

As an **admin**,
I want a single dashboard that shows me LLM spend, message volume, and HITL load with a clear "wasted spend" callout and the ability to drill into individual calls,
So that I can understand cost drivers and trace surprising costs back to specific moments.

**Acceptance Criteria:**

**Given** an admin loads `/admin/usage?project_id=1&window=1w`
**When** `usage_daily_summary` has 7 days of data for project 1
**Then** the dashboard renders three tracker tiles, a Wasted-spend tile, and line/sparkline charts
**And** the chart-rendering path makes zero queries against the raw tables (summary-first verified by query-counter test).

**Given** an admin in `Europe/Moscow` timezone
**When** they view the 1d window
**Then** "today" cuts at local 00:00 MSK (not 00:00 UTC).

**Given** an operator loads the same page
**When** the page renders
**Then** the Wasted-spend tile is hidden
**And** the DOM contains no `$`-formatted cost text.

Full ACs: [story-14-06-web-ui-usage-dashboard.md](epics/stories/epic-14/story-14-06-web-ui-usage-dashboard.md).

### Story 14.07: Usage API endpoints + money RBAC (admin vs operator scope)

As an **admin**,
I want APIs that return per-project usage summaries with cost data,
So that the dashboard and `/usage` bot command can render accurate financials.

As an **operator**,
I want APIs that return per-project token + volume data with cost data stripped at the server,
So that I can monitor activity without seeing money the platform considers privileged.

**⚠️ Gated on Epic 10.5 shipping first.**

**Acceptance Criteria:**

**Given** an admin calls `GET /api/usage/summary?project_id=1&from=...&to=...&trackers=all`
**When** the request succeeds
**Then** the response contains `cost_usd_total` non-NULL fields per LLM row.

**Given** an operator-for-project-1 calls the same endpoint
**When** the request succeeds
**Then** the response Pydantic model has NO `cost_usd` keys at all (byte-clean)
**And** the captured SQL `SELECT` list does not include `cost_usd_total` (defense-in-depth verified).

**Given** an operator-for-project-2 calls `?project_id=1`
**When** the request is processed
**Then** the response is 403 `not_authorized_for_project`.

**Given** an operator calls `/api/usage/wasted`
**When** the request is processed
**Then** the response is 403 `wasted_endpoint_admin_only`.

Full ACs: [story-14-07-usage-api-and-money-rbac.md](epics/stories/epic-14/story-14-07-usage-api-and-money-rbac.md).

### Story 14.08: `/usage` bot command (role-aware, three-tracker output, deep link)

As an **admin**,
I want to type `/usage` in Telegram and see today's LLM cost, message volume, and HITL events for my project,
So that I can do a quick spend check without opening the dashboard.

As an **operator**,
I want to type `/usage` and see today's token + message + HITL counts (no money),
So that I can gauge load without seeing privileged data.

**⚠️ Gated on Epic 10.5 + Story 14.07 shipping first.**

**Acceptance Criteria:**

**Given** a registered operator types `/usage`
**When** the bot processes the command
**Then** a DM arrives containing tokens, messages, and HITL counts for their assigned project
**And** the DM body contains zero `$` characters and zero `Расход` / `Потрачено впустую` substrings.

**Given** an admin types `/usage`
**When** the bot processes
**Then** the DM contains formatted cost figures + Wasted-spend line + a deep link to `/admin/usage?project_id=<id>&window=1d`.

**Given** a non-registered Telegram user types `/usage`
**When** the bot processes
**Then** no DM is sent
**And** `unauthorized_usage` is logged.

Full ACs: [story-14-08-usage-bot-command.md](epics/stories/epic-14/story-14-08-usage-bot-command.md).

### Story 14.09: Alerting (triple-indicator) + incident state machine

As an **admin**,
I want to be DM'd ONCE when a cost / volume / HITL spike starts, told what's expanding when more trackers join, and told when the incident ends — not pinged 24 times a day,
So that alerts stay actionable and I trust the channel.

**Acceptance Criteria:**

**Given** a project with `daily_budget_cap_usd = $10`
**When** today's LLM cost crosses $8 (80%)
**Then** ONE `INCIDENT_START` DM arrives containing the project name, breached tracker, and peak percentage
**And** the same indicator does NOT re-fire on subsequent scheduler ticks.

**Given** the same project's message volume also breaches its rolling-avg threshold 30 minutes later
**When** the alerter runs
**Then** ONE `INCIDENT_EXPAND` DM arrives indicating the additional tracker is now also breaching
**And** the `usage_incidents.breached_trackers` JSON includes both trackers.

**Given** all breached trackers return under threshold
**When** they remain under for ≥60 minutes continuous
**Then** ONE `INCIDENT_END` DM arrives with duration, peak %, and total excess cost
**And** the incident appears in the Alerts tab with fingerprint `usage:<project_id>:<incident_id>` and a START/EXPAND/END timeline.

**Given** a project is on day 7 with `today_cost=$15` vs `7d_avg=$5`
**When** the alerter runs
**Then** no rolling-avg fire occurs (bootstrap gate).
**And** on day 8 with the same values, exactly one `llm_cost_rolling_avg_breach` fires.

**Given** a single inbound message aggregates `$1.50` of LLM cost across multiple LLM calls sharing one `trace_id`
**When** the last LLM call's row is written
**Then** exactly one `cost_outlier_per_message` fires.

Full ACs: [story-14-09-alerting-and-incident-state-machine.md](epics/stories/epic-14/story-14-09-alerting-and-incident-state-machine.md).

### Story 14.10: Epic signoff (backup runbook, alert-threshold config, e2e, project-cap backfill)

As a **platform operator**,
I want Epic 14 to be backup-safe, configurable by admin, gracefully rolled out to existing projects, and demonstrably correct end-to-end,
So that I can ship it to production with confidence.

**Acceptance Criteria:**

**Given** the Epic 14 backfill migration runs against a deployment with 5 existing projects
**When** 2 already have a custom `usage_daily_budget_cap_usd` and 3 do not
**Then** the 3 projects without a cap receive the default ($25); the 2 with custom caps are untouched
**And** each backfilled project's admin receives one Telegram DM with the cap value and a config link
**And** each backfilled project has `usage_alert_grace_period_until = now + 30 days`.

**Given** the backup script runs against a fresh `.data/` with all DBs present
**When** the tar.gz is created
**Then** it includes `semantaix_usage.db`
**And** restoring from it recovers daily-summary rows.

**Given** an admin types `/usage_config set daily_budget_cap_usd 50`
**When** the bot processes the command
**Then** `hitl_runtime_config` updates to the new value
**And** the alerter uses the new threshold on the next tick.

**Given** `scripts/epic14_signoff.sh` runs
**When** the script completes
**Then** `ruff check .` passes, `pytest --cov` shows 100% line coverage on Epic 14 modules, `pytest -m e2e` is green, and the golden-traffic smoke test exercises all three trackers + dashboard + `/usage` + incident lifecycle.

Full ACs: [story-14-10-epic-14-signoff.md](epics/stories/epic-14/story-14-10-epic-14-signoff.md).
