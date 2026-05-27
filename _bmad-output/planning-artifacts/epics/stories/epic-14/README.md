# Epic 14 Story Pack

Epic: Usage / Token / Cost Monitoring + Cost-Spike Alerting

This story pack is implementation-ready and includes, per story:
- scope boundaries (in/out)
- implementation notes grounded in the architecture + project-context rules
- test requirements (unit / contract / integration)
- automated E2E + manual verification
- completion gates (100% coverage on new modules, ruff clean, money RBAC byte-cleanness verified at API + bot surfaces, usage-write failure never raises into the user-facing pipeline)

**One PR per story** (per `epics/README.md` rule). Each story branch maps 1:1 to the BMAD `create-story → dev-story → code-review → PR` cycle.

Implementation order follows the dependency graph. 14.01 is the foundation and blocks every later story. Stories 14.07 (API + RBAC) and 14.08 (`/usage` bot command) are **gated on Epic 10.5 (operator/project model refinement) shipping first**; the other stories can proceed in parallel:

```
14.01 schema + migration + WAL + indexes (semantaix_usage.db, 5 tables)   ← foundation, blocks all
  ├── 14.02 OpenRouter LLM-call instrumentation + UsageRecorder seam + call_outcome enum (scattered)
  ├── 14.03 message-volume instrumentation (bot_gateway, in/out events)
  ├── 14.04 HITL event instrumentation (api, lifecycle: created/assigned/replied/resolved)
  │
  └── 14.05 scheduler daily roll-up worker + 30d raw retention purge   (requires 14.02 + 14.03 + 14.04)
        ├── 14.06 Web UI usage dashboard (charts, drill-down, Wasted-spend tile, browser-tz rendering)
        ├── 14.07 API endpoints + money RBAC (admin vs operator scope)   ⓘ gated on Epic 10.5
        ├── 14.08 /usage bot command (role-aware three-tracker output)   ⓘ gated on Epic 10.5 + 14.07
        ├── 14.09 alerting: triple-indicator + incident state machine (INCIDENT_START/EXPAND/END)
        │
        └── 14.10 epic signoff: backup runbook update, alert-threshold config UI, e2e tests, project-cap backfill
```

## Story list
- `story-14-01-usage-db-schema-and-migration.md`
- `story-14-02-llm-instrumentation-and-recorder.md`
- `story-14-03-message-volume-instrumentation.md`
- `story-14-04-hitl-event-instrumentation.md`
- `story-14-05-daily-rollup-and-retention.md`
- `story-14-06-web-ui-usage-dashboard.md`
- `story-14-07-usage-api-and-money-rbac.md`
- `story-14-08-usage-bot-command.md`
- `story-14-09-alerting-and-incident-state-machine.md`
- `story-14-10-epic-14-signoff.md`

## Automated E2E (current repo)
Story-aligned E2E tests land in `tests/e2e/test_e2e_epic14_*.py` (`@pytest.mark.e2e`, `@pytest.mark.epic("14")`, `@pytest.mark.story("14-NN")`). Earliest end-to-end coverage belongs to 14.02 (LLM-call round-trip — customer message → answerer → `usage_llm_calls` row with correct `call_outcome`); the dashboard round-trip lands in 14.06; the money-RBAC byte-cleanness round-trip in 14.07; the `/usage` bot command admin/operator output diff in 14.08; the triple-indicator + incident-state-machine lifecycle in 14.09. CI runs `pytest` with coverage plus `pytest -m e2e`. Story-level rows live in `_bmad-output/implementation-artifacts/e2e-coverage.md`. Scripted signoff: `scripts/epic14_signoff.sh`.

## Carry-forward integration
Per `epics/README.md`, every epic from Epic 03 onward integrates with the Epic 02 incident/alerts solution. Epic 14 alerting (14.09) emits incidents through the existing `incidents` engine with fingerprint `usage:<project_id>:<incident_id>` so Alerts-tab UI + `@ajdevy` critical Telegram notifications surface usage incidents without bespoke channels.
