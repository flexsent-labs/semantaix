# Story 14.10 — Epic 14 signoff: backup runbook, alert-threshold config UI, e2e tests, project-cap backfill

## Objective
Close out Epic 14: extend the Epic 07 backup runbook to cover `semantaix_usage.db`; ship a minimal admin-facing alert-threshold configuration surface (slash command + Settings UI); backfill the required daily budget cap on existing projects with admin notification + 30-day grace window; complete the e2e coverage matrix in `_bmad-output/implementation-artifacts/e2e-coverage.md`; ship the `scripts/epic14_signoff.sh` CI-parity script; perform the final acceptance demo against a live container.

**As a** platform operator,
**I want** Epic 14 to be backup-safe, configurable by admin, gracefully rolled out to existing projects, and demonstrably correct end-to-end,
**So that** I can ship it to production with confidence.

PRD reference: **NFR-11** (Usage Backup Coverage), FR-35 alert-threshold config + project-cap backfill, all Epic 14 release-readiness criteria in PRD §9.

## Scope

### In Scope
- **Backup runbook extension (Epic 07 carry-forward)**:
  - Update `scripts/backup_*.sh` (or its successor) to include `.data/semantaix_usage.db` in the tar.gz archive list.
  - Update the `backups` table's metadata to record presence/size of the usage DB in each backup row.
  - Update the restore flow to handle `semantaix_usage.db` alongside the other stores (idempotent — if the file exists, replace it; if not, restore from archive).
  - Restore-test: e2e test creates a backup, deletes `.data/semantaix_usage.db`, restores → daily-summary rows are recovered (raw rows older than 30 days remain purged per retention policy — that's by design).
  - Document the inclusion in `_bmad-output/implementation-artifacts/backup-runbook.md` (or the existing runbook doc).
- **Project-cap backfill** (one-time at Epic 14 deployment):
  - Migration script `scripts/migrations/2026_epic14_backfill_budget_caps.py`:
    - For every project in `semantaix_projects.db` without an explicit `usage_daily_budget_cap_usd` in `hitl_runtime_config`, set a default ("$25.00" — configurable via env `EPIC14_DEFAULT_BUDGET_CAP_USD`).
    - DM each project's registered admin (one DM per project) with the assigned default + a link to `/admin/usage/config?project_id=<id>` to override.
    - Set `usage_alert_grace_period_until` = `now + 30 days` per project. The alerter (14.09) SKIPS firing budget-cap alerts before the grace period ends — gives admins time to right-size the cap.
  - Idempotent: re-running the migration is a no-op (skips projects that already have a non-default cap).
- **Alert-threshold configuration surface** (minimal):
  - **Slash command** `/usage_config show|set <key> <value>` for admins:
    - `show` — DM the current per-project config: cap, warning/breach %s, outlier $, rolling-avg coefficients.
    - `set <key> <value>` — update one threshold in `hitl_runtime_config`. Allowed keys per FR-35: `daily_budget_cap_usd`, `cap_warning_pct`, `cap_breach_pct`, `outlier_per_message_usd`, `rolling_avg_pct`, `rolling_avg_floor_llm_usd`, `rolling_avg_floor_messages`, `rolling_avg_floor_hitl`, `incident_hysteresis_minutes`.
    - Validates value type/range (e.g. `daily_budget_cap_usd > 0`, `cap_warning_pct in [1, 100]`); DMs an error on invalid input.
    - Admin-only; non-admin → ignored with logged `unauthorized_usage_config`.
  - **Web UI Settings page** `/admin/usage/config?project_id=<id>` — same surface as the slash command but with a form. Admin-only. Reads + writes `hitl_runtime_config`.
- **`scripts/epic14_signoff.sh`** — CI-parity signoff:
  - `ruff check .`.
  - `pytest --cov --cov-config=.coveragerc --cov-report=term-missing` (100% gate on new modules).
  - `pytest -m e2e` (all `@pytest.mark.epic("14")` tests).
  - Smoke: spin up a docker-compose stack, run a "golden traffic" script that generates LLM calls + messages + HITL escalations + a synthetic budget-cap breach, verify dashboard + `/usage` outputs + incident DMs all match expectations.
- **`_bmad-output/implementation-artifacts/e2e-coverage.md`** update — one row per Epic 14 story (14.01–14.10) with the e2e file paths + the assertions covered.
- **Per-project `EPIC14_DEFAULT_BUDGET_CAP_USD` setting** in `platform_common/settings.py`, default `$25.00`, documented in `.env.example`.
- **Final demo against a live container**: drive a Telegram conversation through Semantaix that triggers all three trackers; load the dashboard; run `/usage` as admin and as operator; force a synthetic budget-cap breach via `/usage_config set daily_budget_cap_usd 0.05` (low cap) + a single LLM call; verify the incident lifecycle DMs.
- **Release notes / changelog update** — `CHANGELOG.md` (or wherever release notes live) gets an Epic 14 entry summarizing the new dashboard, `/usage` command, alerting, and the cap-backfill rollout.

### Out of Scope
- Multi-currency support (USD only — accepted per FR-35).
- Multi-project rollup config (per-project only).
- Email or webhook alert delivery channels (admin Telegram DM only).
- Real-time dashboard refresh (page-reload-based per 14.06).
- Backfill of historical raw rows (none — fresh-start per FR-31).
- Cleanup of any deprecated Epic 14 code (none introduced; nothing to clean up).
- Audit log of who changed alert thresholds — covered by structured logs on `hitl_runtime_config` writes (existing pattern).

## Implementation Notes
- **Backup runbook idempotency** — running the backup script before and after Epic 14 produces the right behavior: pre-deploy archives lack `semantaix_usage.db` (correct — file didn't exist); post-deploy archives include it. Restore from a pre-deploy archive leaves the deployed instance with its current `semantaix_usage.db` (since the file is absent from the archive, restore skips it; deployed instance retains today's data). Document this nuance.
- **Backfill grace-period interplay** — `usage_alert_grace_period_until` is checked in the alerter (14.09 modification): if `now < grace_until`, skip budget-cap fires for the project (rolling-avg + outlier fires still happen — those don't depend on the admin-set cap). This story patches the alerter to add the check.
- **Cap-backfill DM template** — Russian; lives in `data/russian_usage_strings.json`. Example: `"Эпик 14 (мониторинг расходов) запущен. Для проекта <name> установлен дневной лимит $25 на 30 дней. Измените в /admin/usage/config или командой /usage_config set daily_budget_cap_usd <value>."`.
- **`/usage_config` command** — start-of-message anchored regex; admin-gated. Reuse the existing admin-command gate from `_handle_admin_hitl_command`. Strict validation per value type/range; the source-of-truth for allowed keys is a frozen dataclass `UsageConfigSchema` so adding a new threshold requires one place to update.
- **Web UI form** — minimal HTML form posting to `POST /api/usage/config?project_id=`. The api endpoint validates + writes to `hitl_runtime_config`. Mounted under the same admin auth as other settings pages.
- **Signoff script structure** — mirrors `scripts/epic13_signoff.sh` (or whatever the existing pattern is). Single bash script; exit non-zero on any failure; tee output to `_bmad-output/implementation-artifacts/epic14-signoff-<ISO_DATE>.log`.
- **Golden-traffic test script** — `tests/e2e/test_e2e_epic14_signoff_golden.py` — a single test parametrized to walk through the full lifecycle: ingest, roll-up trigger, dashboard query, `/usage` invocation, threshold breach, incident lifecycle.
- **e2e-coverage.md update** — append the Epic 14 rows; keep alphabetical / numeric ordering consistent with the file's existing structure.
- **Release notes** — concise, user-facing language. Highlight new admin command, new dashboard route, the always-on nature, and the 30-day grace period.

## Test Plan

### Unit
- `tests/test_backup_includes_usage_db.py`:
  - Run the backup script against a fresh `.data/` with all DBs present → tar.gz contains `semantaix_usage.db`.
  - Restore from the archive → `semantaix_usage.db` is back; daily-summary rows are recovered.
  - Backup without `semantaix_usage.db` present → archive succeeds; restore from it → leaves any existing `semantaix_usage.db` intact (no overwrite).
- `tests/test_budget_cap_backfill_migration.py`:
  - Seed `semantaix_projects.db` with 5 projects, 2 of which already have `usage_daily_budget_cap_usd` in `hitl_runtime_config` → migration adds default cap to the OTHER 3 projects; the existing 2 are untouched.
  - Re-running migration → idempotent (no double writes, no DMs sent twice).
  - Each backfilled project receives one Telegram DM to its registered admin.
- `tests/test_usage_config_slash_command.py`:
  - `/usage_config show` as admin → DM with all current values.
  - `/usage_config set daily_budget_cap_usd 50` → writes to `hitl_runtime_config`; DM confirms.
  - `/usage_config set daily_budget_cap_usd -5` → DMs validation error; no write.
  - `/usage_config set bogus_key 1` → DMs "unknown key"; no write.
  - Non-admin `/usage_config` → no DM; logged `unauthorized_usage_config`.
- `tests/test_usage_config_api_endpoint.py`:
  - `POST /api/usage/config` admin → 200 + value written.
  - Same as operator → 403.
- `tests/test_alerter_grace_period.py`:
  - Project with `usage_alert_grace_period_until > now` → budget-cap fires SKIPPED; rolling-avg fires NOT skipped.
  - Project with `usage_alert_grace_period_until < now` (expired) → all fires active.

### Contract
- `tests/contract/test_usage_config_endpoint_contract.py` — assert the config endpoint's request/response shape.

### Integration
- `tests/test_epic14_signoff_smoke.py` — golden-traffic walkthrough that exercises all the above in one shot.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_signoff_golden.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-10")`):
  - Full lifecycle in one test: deploy → backfill → cap configured → dashboard renders → `/usage` admin/operator → cap-warning fire → incident-expand → incident-end → backup → restore → dashboard renders post-restore.

## Manual Verification
1. **Backup**: `scripts/backup_now.sh` (or equivalent) → confirm tar.gz includes `semantaix_usage.db`; the Web UI Backups tab shows the new backup with its size.
2. **Restore**: stop the stack, delete `.data/semantaix_usage.db`, run the restore flow → file is back; `/admin/usage` renders the recovered summary rows.
3. **Backfill**: deploy Epic 14 to a project that doesn't have a cap → confirm the admin receives the backfill DM with the default $25 cap; confirm `/admin/usage/config?project_id=<id>` shows the assigned value; confirm no budget-cap alerts fire for 30 days.
4. **Config command**: `/usage_config set daily_budget_cap_usd 100` as admin → confirm DM "Дневной лимит установлен: $100" (or equivalent); confirm next-tick alerter uses the new value.
5. **Signoff script**: `bash scripts/epic14_signoff.sh` → all green; coverage 100% on Epic 14 modules; e2e tests pass.
6. **Live demo**: walk a Telegram conversation through the full flow per the brainstorm "key decisions"; record screenshots in `_bmad-output/implementation-artifacts/epic14-demo-<ISO_DATE>.md`.

## Done Criteria
- 100% line coverage on the new admin endpoints + slash command + backfill migration + backup runbook changes.
- `ruff check .` passes.
- `scripts/epic14_signoff.sh` exits zero.
- `pytest -m e2e` includes all Epic 14 stories; `e2e-coverage.md` matrix updated.
- Backfill migration is idempotent; existing projects with custom caps are not overwritten.
- Backup + restore round-trip verified for `semantaix_usage.db`.
- Grace-period check verified — fresh projects don't get bombarded with cap alerts.
- Release notes added.
- Demo against a live container completed and recorded.
- Epic 02 / Epic 07 carry-forward integrations verified: usage incidents in Alerts tab; usage DB in backup tar.gz.
- Epic 10.5 + stories 14.01–14.09 all merged before this story merges (final epic closure).
