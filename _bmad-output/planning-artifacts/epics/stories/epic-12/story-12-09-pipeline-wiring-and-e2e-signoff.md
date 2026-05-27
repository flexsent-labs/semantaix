# Story 12.09 — Pipeline wiring, always-on activation, and e2e signoff

## Objective
Tie the epic together: insert `SalesPersonaAnswerer` into `AnswerPipeline` (before `CalendarAvailabilityAnswerer`), confirm the always-on activation model (the answerer's only gates are an existing conversation-state row OR a sales-intent regex match — no project-level enable, no services-count check), ship `scripts/epic12_signoff.sh`, add Epic-12 rows to `e2e-coverage.md`, and run the full Данил dialog replay as the acceptance signal. After this story, Epic 12 is operationally complete.

## Scope

### In Scope
- **Pipeline insertion** in `services/api/app/main.py`: place `SalesPersonaAnswerer` **before** `CalendarAvailabilityAnswerer` in the `AnswerPipeline([...])` list assembled around L202. Order is the routing logic: `DateTimeAnswerer (legacy seam) → HolidayAnswerer → WeatherAnswerer → SalesPersonaAnswerer → CalendarAvailabilityAnswerer → GroundedRagAnswerer → HITL`.
- **Activation gate** (already implemented in `SalesPersonaAnswerer.try_answer` per story 12.03; this story is the regression test that it stays gated correctly):
  - A `sales_conversation_state` row exists for the chat → enters (regardless of services count).
  - Or: inbound text matches the sales-intent regex → enters (regardless of services count).
  - Otherwise → `_skip(reason="not_sales_intent")` (silent fall-through to RAG/HITL).
  - **No `services_repo.count_active` gate.** Sales is always-on — a project with zero services can still scope, look up prices via RAG, and propose calendar dates.
- `scripts/epic12_signoff.sh` — orchestrates the live signoff:
  1. `docker compose up -d`.
  2. Seed `/service_add Медовеевка Лайт | Лайт уровень, с видами` and `/service_add каньонинг | Каньонинг — это…` via the bot_gateway.
  3. Forward a sample MP4 + `/material` to register one tour-preview material; upload `tour_catalog.pdf` to `/kb_add` to verify the auto-promotion path (asserts a second material row appears).
  4. POST the Данил inbound messages in order to `/conversations/inbound`.
  5. Assert: bot intent-scoping turns persist into `sales_conversation_state`; autonomous material dispatch happens after scoping completes; price ask with empty KB → HITL ticket with `reason='price_unknown'` + customer-facing fixed line "Уточню у коллег и сразу сообщу".
  6. Operator HITL reply → assert a `knowledge_moderation` candidate is auto-created (Epic-06 extractor handoff); approve it via the moderation endpoint.
  7. POST the same price ask again → assert bot quotes the learned price (with `source_chunk_id` in the answer-trace metadata) and no new HITL ticket.
  8. Fast-forward the clock 25h with no customer reply → scheduler tick → assert one Telegram nudge fires in daytime hours.
  9. Reset; seed `intent.dates` to a past date → tick → assert `status='skipped_stale'`, no message sent.
- **`e2e-coverage.md` rows.** One row per story (12.01 through 12.09 + 12.05b) mapping `pytest::nodeid` to the story id.
- **Validation tests** that exist purely to defend the ordering + intent-gate invariants:
  - `tests/test_pipeline_order_includes_sales_before_calendar.py` — the live `AnswerPipeline` instance has `SalesPersonaAnswerer` immediately before `CalendarAvailabilityAnswerer`.
  - `tests/test_pipeline_sales_active_on_empty_services.py` — with **zero** `services` rows for a project, an inbound message matching a sales-intent lemma still enters the sales answerer (regression guard for the always-on invariant). The bot proceeds through greeting/scoping even with an empty catalog.
  - `tests/test_pipeline_sales_silent_on_non_intent.py` — without an existing conversation state and without a sales-intent match, the sales answerer skips silently (no LLM call, no DB write).
- **Log-capture security test** `tests/test_sales_no_secrets_in_logs.py` — runs a full inbound + dispatch + HITL escalation pass and asserts that the captured `caplog.text` contains zero occurrences of: any `Settings.internal_service_token`, any `telegram_file_id` from the seeded materials, any `Settings.openrouter_api_key`, and zero raw price-payload `original_question` echoes (a stricter check than the existing log-capture tests).
- **Sprint-status update.** `_bmad-output/implementation-artifacts/sprint-status.yaml` flips `epic-12` to `done` only after this story's PR merges; before then this story flips it to `in-progress`.

### Out of Scope
- New answerer behavior (everything ships in 12.03 through 12.08). This story is wiring + signoff + regression nets.
- Operator-facing dormancy command (`/sales_off` to silence the funnel without removing services) — not in v1.
- Migration scripts (a fresh `docker compose up` bootstraps `.data/semantaix_sales.db` empty; existing deployments simply gain an empty new DB).
- Performance benchmarks beyond the implicit "no added latency when dormant" assertion in the regression test.

## Implementation Notes
- **Pipeline order is the routing logic** (project-context). The `AnswerPipeline([...])` list is the single source of truth — this is the only place to edit. Insert `SalesPersonaAnswerer` immediately before `CalendarAvailabilityAnswerer`; do not relocate the calendar answerer or any other entry.
- **Dormancy is enforced at the answerer**, not at the pipeline. The pipeline always asks the sales answerer to `try_answer`; the answerer's first action is the activation gate (cheap; one repo call). When dormant, no LLM call, no RAG call, no DB write — just `_skip(reason=...)` + log.
- **Signoff script is bash + curl + sqlite3** (mirror `scripts/epic11_signoff.sh`). No new shell deps. Tokens for the internal endpoints are read from the running `.env`.
- **Clock fast-forward in signoff** is achieved by hitting a dev-only `POST /sales/_dev/tick-followup-now` endpoint that the api exposes ONLY when `Settings.environment == "dev"` — this avoids a fragile bash sleep + system-clock manipulation. The endpoint is rejected with 404 when `environment != "dev"` (asserted by a unit test).
- **`_bmad-output/implementation-artifacts/e2e-coverage.md` rows** follow the existing matrix format (one row per story, columns: story, pytest nodeid, behavior). Do not rename columns or reorder existing rows.
- **Persona system-prompt files are checked in** under `services/api/app/sales/system_prompts/`. The seven files exist by this story (greeting, scoping, pitching, pricing_hit, proposal, followup, catalog, concept_rag, kb_material_analyzer); each is short and editable without a redeploy. Verify a presence test in this story: `tests/test_sales_system_prompts_present.py` asserts every expected file exists and is non-empty.

## Test Plan
### Unit
- `tests/test_pipeline_order_includes_sales_before_calendar.py` — described above.
- `tests/test_pipeline_sales_silent_on_non_intent.py` — described above.
- `tests/test_sales_no_secrets_in_logs.py` — described above.
- `tests/test_sales_system_prompts_present.py` — file-existence + non-empty for every prompt file referenced by the answerer.
- `tests/test_sales_dev_tick_endpoint_gated.py` — `POST /sales/_dev/tick-followup-now` returns 404 outside `environment="dev"`; 200 inside.

### Integration
- `tests/test_pipeline_sales_active_full_funnel.py` — seed services + materials + RAG price chunk + Epic-11 calendar stub; replay the Данил inbound messages; assert the full sequence: greeting → scoping (5 fields) → media dispatch → pricing hit → date proposal → acceptance → closing handoff ticket.

## Automated E2E verification
- `tests/e2e/test_e2e_epic12_full_danil_dialog.py` (`@pytest.mark.e2e`, `@pytest.mark.epic("12")`, `@pytest.mark.story("12-09")`) — the full Данил replay, the acceptance signal for the epic.
- `tests/e2e/test_e2e_epic12_active_on_empty_catalog.py` — a project with zero services receives a sales-intent message → the bot greets + scopes (NOT silent); answer trace shows `sales_persona` handled the turn. The catalog turn ("Что у вас есть?") in this state replies "Услуг пока нет. Уточню у коллег и сразу сообщу." and escalates.

## Manual Verification
1. `bash scripts/epic12_signoff.sh` — green run; the script prints a one-line PASS per assertion.
2. As a customer on a freshly-bootstrapped project (no `services` rows, no KB files yet): send "интересует тур на квадроциклах 1 мая" → expect the **greeting + scoping turn** (always-on activation verified — sales runs even before any service is configured).
3. Send a non-sales message ("какая погода в Сочи?") → expect the existing RAG/weather behavior; the sales answerer skipped silently.
4. Walk through the full Данил script manually; confirm each behavior in the exit criteria.

## Done Criteria
- 100% coverage on `services/api/app/sales/` and on the new bot_gateway / scheduler glue.
- `ruff check .` passes; `pytest -m e2e` green for all Epic-12 markers.
- `scripts/epic12_signoff.sh` exits 0; `bash scripts/run_all_epic_feature_signoffs.sh` exits 0.
- `_bmad-output/implementation-artifacts/e2e-coverage.md` updated with one row per Epic-12 story.
- `_bmad-output/implementation-artifacts/sprint-status.yaml` reflects `epic-12: done`.
- The always-on regression test proves the sales answerer engages on sales-intent messages even when the project has zero `services` rows. The non-intent regression test proves it remains silent on non-sales messages (no LLM cost, no DB write).
- No API key, service token, `telegram_file_id`, or raw price-payload `original_question` appears in logs or `answer_traces` metadata (log-capture assertion).
- Pipeline order: `SalesPersonaAnswerer` is the answerer immediately before `CalendarAvailabilityAnswerer`; regression-tested.
