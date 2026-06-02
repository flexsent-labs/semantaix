# Story 12.42: DM the admin when a configured LLM model is unavailable (P1)

Status: review

## Story

As an **operator**,
I want a **Telegram alert the moment the bot detects a configured model is gone**,
so that **a retired model never silently breaks every reply for days (rounds 6–7) before anyone notices.**

**Context:** Story 12.41 added detection (startup validation + `/health/model`) and the right model. But detection only *logs* + returns 503 — nobody is paged. The round-6/7 outage went unnoticed across multiple QA rounds precisely because the failure was silent. This story closes that: a model problem now **DMs the admin**.

## Acceptance Criteria

1. When the startup guard finds a configured model OpenRouter no longer serves, the admin gets a Telegram DM (the `telegram_alert_chat_id` operator) describing which model(s) and that the bot will degrade until fixed.
2. The alert flows through the **existing critical-incident path** (`incident_repository.ingest` → `TelegramIncidentNotifier.notify_if_critical`), so it inherits the existing **dedup (incident window) + debounce (`telegram_alert_debounce_seconds`)** — repeated restarts during an outage don't spam.
3. No alert when all configured models are available.
4. Unconfigured key (unit tests) → no network, no alert (the startup guard already skips).
5. The `/incidents/events` endpoint behaviour is unchanged (the ingest+notify+debounce logic was extracted into a shared helper, not altered).
6. Gates green; 100% coverage.

## Design

- New critical fingerprint **`llm_model_unavailable`** in `TelegramIncidentNotifier.CRITICAL_FINGERPRINTS` — a retired model is a critical infra incident, like `provider5xx_spike`.
- Extracted `main._record_and_alert_incident(fingerprint, severity, summary)` — the exact ingest + `is_critical_event` + debounce + `notify_if_critical` + `append_event` logic that already lived inline in `/incidents/events` (verbatim branch structure, so the endpoint's tests still cover it). The endpoint now delegates to it.
- The startup guard (`validate_llm_models_on_startup`, Story 12.41) calls `_record_and_alert_incident(fingerprint="llm_model_unavailable", severity="critical", summary=…)` when models are missing → DMs the admin (deduped/debounced).

## Out of scope (documented follow-ups)

- **Real-time alert during traffic:** alert the moment a persona `complete_json` 404s (`sales_llm_transport_error`), catching a *mid-uptime* deprecation before the next restart. Needs the incident/notify seam injected into `SalesPersonaAnswerer` — a separate change. (Startup-time alerting already covers every deploy/restart, which is the operator's frequent QA cadence.)
- **Periodic check:** a scheduler job that polls `/health/model` and alerts, for long-running instances with no restarts. `/health/model` is intentionally side-effect-free (a health probe shouldn't DM), so the periodic *trigger* belongs in the scheduler.

## Tasks / Subtasks

- [x] Add `llm_model_unavailable` to `CRITICAL_FINGERPRINTS`.
- [x] Extract `_record_and_alert_incident` from `/incidents/events`; endpoint delegates to it.
- [x] Startup guard raises the critical incident (→ admin DM) when a model is unavailable.
- [x] Tests (TDD): fingerprint is critical; startup alerts with the right fingerprint/severity/summary on a dead model; no alert when healthy; endpoint contract unchanged.

## Dev Notes

- **Why the incident path (not a direct DM):** it reuses the proven dedup + debounce + token/chat gating, and surfaces the problem in the incident timeline/console alongside other infra incidents — consistent operability.
- **Test safety:** the startup guard makes a network call only when the OpenRouter key is configured (placeholder in tests → skips), and `notify_if_critical` no-ops on an unconfigured bot token — so the suite never DMs / hits the network.
- **Files:** `services/api/app/telegram_notifier.py`, `services/api/app/main.py`.

## References

- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-round6-blockers-investigation.md` (round-7 dead-model root cause).
- Builds on Story 12.41 (model fix + detection guard).
- Reuses: `TelegramIncidentNotifier` (`telegram_notifier.py`), the `/incidents/events` debounce path, `telegram_alert_chat_id` / `telegram_alert_debounce_seconds`.

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–6).** A configured-model deprecation now DMs the admin via the critical-incident path (deduped/debounced), so the silent-breakage that cost rounds 6–7 can't recur unnoticed across a restart.
- **Refactor, not rewrite:** extracted the existing ingest+notify+debounce logic into `_record_and_alert_incident`; the `/incidents/events` endpoint delegates to it (its tests stay green, coverage preserved).
- **TDD; 100% coverage**; full suite green.

### File List

- `services/api/app/telegram_notifier.py` (modified — new critical fingerprint)
- `services/api/app/main.py` (modified — `_record_and_alert_incident` helper + startup alert)
- `tests/test_telegram_notifier.py` (modified), `tests/test_api_health_model.py` (modified — startup alert tests)
- `_bmad-output/implementation-artifacts/12-42-alert-admin-on-model-problem.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
