# Story-aligned E2E coverage (pytest)

## Definition

“E2E” in this repository means **multi-step integration tests** over the real FastAPI application graph: `fastapi.testclient.TestClient`, **ephemeral SQLite** databases via patched repository paths / env vars, and **mocked external services** (OpenRouter, Telegram send). Tests run in **GitHub Actions** on every PR and push to `main`.

Browser automation is **not** used. Admin HTML in `web_ui` has an HTTP smoke check for the shell, plus the Epic 08 trace-UI E2E (`test_e2e_epic08_trace_ui.py`) that renders sources/policy/routing/confidence for a persisted trace.

## How to run

- Full suite (unit + contract + E2E): `pytest`
- Coverage (same gates as CI): `pytest --cov --cov-config=.coveragerc --cov-report=term-missing`
- E2E marker subset only: `pytest -m e2e`

Markers are declared in `[tool.pytest.ini_options]` in [`pyproject.toml`](../../pyproject.toml).

## Coverage matrix

Each row marks whether a happy path (`H`) and/or an error/incident path (`E`) is covered. Test IDs are pytest node ids; subset under `tests/e2e/` is the e2e-marked suite.

| Epic | Story / area | Scenario | H / E | Primary test ID |
|------|--------------|----------|-------|-----------------|
| 01 | 01-01 | Webhook accepts text update + returns trace | H | `tests/test_bot_gateway_webhook.py::test_webhook_accepts_text_message_and_returns_trace` |
| 01 | 01-02 | Webhook persists conversation + message row | H | `tests/test_bot_gateway_webhook.py::test_webhook_persists_message_rows` |
| 01 | 01-03 | `/suggest` suggestion payload via mocked LLM | H | `tests/test_api_suggest_contract.py::test_suggest_returns_suggestion_payload_on_success` |
| 01 | 01-04 | Webhook persist + `/suggest` cross-service | H | `tests/test_epic01_e2e.py::test_epic01_e2e_webhook_persist_suggest` |
| 01 | 01-04 | Webhook persist + RAG retrieval grounded `/suggest` | H | `tests/e2e/test_e2e_epic01_chain.py::test_epic01_e2e_webhook_persist_then_suggest_with_retrieval` |
| 01 | 01-04 | `/suggest` 503 when OpenRouter key missing | E | `tests/e2e/test_e2e_epic01_chain.py::test_epic01_e2e_suggest_returns_503_when_openrouter_key_missing` |
| 01 | 01-04 | `/suggest` 502 on provider failure | E | `tests/e2e/test_e2e_epic01_chain.py::test_epic01_e2e_suggest_returns_502_on_provider_failure` |
| 02 | 02-02 | Ingest → list → read → ack → resolve → timeline | H | `tests/e2e/test_e2e_epic02_incident_lifecycle.py::test_epic02_full_lifecycle_emit_read_ack_resolve_timeline` |
| 02 | 02-01 | Dedup within window collapses + `deduplicated` event | H | `tests/e2e/test_e2e_epic02_incident_lifecycle.py::test_epic02_dedup_within_window_collapses` |
| 02 | 02-01 | Dedup outside window auto-resolves prior + creates new | E | `tests/e2e/test_e2e_epic02_incident_lifecycle.py::test_epic02_dedup_outside_window_creates_new_and_auto_resolves_prior` |
| 02 | 02-03 | Critical Telegram alert sent + debounced + sent again | H | `tests/e2e/test_e2e_epic02_incident_lifecycle.py::test_epic02_critical_telegram_debounce` |
| 02 | 02-03 | Warning event does not page Telegram | H | `tests/e2e/test_e2e_epic02_incident_lifecycle.py::test_epic02_warning_does_not_send_telegram` |
| 03 | 03-01 | Valid suggestion passes guardrails | H | `tests/e2e/test_e2e_epic03_guardrails.py::test_epic03_valid_suggestion_passes_no_ticket_or_incident` |
| 03 | 03-01 | Low confidence blocks → incident + HITL ticket | E | `tests/e2e/test_e2e_epic03_guardrails.py::test_epic03_low_confidence_blocks_emits_incident_creates_ticket` |
| 03 | 03-01 | Policy violation blocks | E | `tests/e2e/test_e2e_epic03_guardrails.py::test_epic03_policy_violation_blocks` |
| 03 | 03-01 | Too-long response blocks | E | `tests/e2e/test_e2e_epic03_guardrails.py::test_epic03_too_long_response_blocks` |
| 03 | 03-01 | Insufficient content blocks | E | `tests/e2e/test_e2e_epic03_guardrails.py::test_epic03_insufficient_content_blocks` |
| 04 | 04-01 | Blocked suggest → route → resolve | H | `tests/e2e/test_e2e_epic04_hitl_journey.py::test_epic04_guardrail_blocked_suggest_then_route_and_resolve` |
| 04 | 04-02 | Bot-authored reply delivered via mocked Telegram | H | `tests/e2e/test_e2e_epic04_hitl_journey.py::test_epic04_full_bot_authored_reply_chain` |
| 04 | 04-01 | Route without operator → 503 + incident | E | `tests/e2e/test_e2e_epic04_hitl_journey.py::test_epic04_route_without_operator_emits_incident` |
| 04 | 04-02 | Reply missing target chat id → 503 + incident | E | `tests/e2e/test_e2e_epic04_hitl_journey.py::test_epic04_reply_missing_target_chat_id_emits_incident` |
| 04 | 04-02 | Reply rejects non-assigned operator → 403 | E | `tests/e2e/test_e2e_epic04_hitl_journey.py::test_epic04_reply_rejects_non_assigned_operator` |
| 04 | runtime-config | `/hitl_config` overrides default operator on next blocked suggest | H | `tests/e2e/test_e2e_epic04_hitl_journey.py::test_epic04_runtime_config_overrides_default_operator` |
| 05 | 05-02 | RAG ingest then `/suggest` returns matching `retrieval` | H | `tests/e2e/test_e2e_epic05_rag_suggest.py::test_epic05_rag_ingest_then_suggest_includes_retrieval` |
| 05 | 05-01 | Repeated ingest dedup returns zero new chunks | H | `tests/e2e/test_e2e_epic05_rag_suggest.py::test_epic05_rag_ingest_dedup_returns_zero_chunks_second_call` |
| 05 | 05-01 | Multi-source retrieve ranks higher overlap first | H | `tests/e2e/test_e2e_epic05_rag_suggest.py::test_epic05_rag_retrieve_ranks_higher_overlap_source_first` |
| 05 | 05-01 | Ingest failure → 500 + incident | E | `tests/e2e/test_e2e_epic05_rag_suggest.py::test_epic05_rag_ingest_failure_emits_incident` |
| 05 | 05-03 | Natural-language intent query scores above grounding threshold against catalog chunk | H | `tests/test_rag_repository.py::test_retrieve_buggy_tour_natural_language_query` |
| 05 | 05-03 | Content-token denominator: short on-topic query scores 1.0 | H | `tests/test_rag_repository.py::test_retrieve_score_uses_content_tokens_denominator` |
| 05 | 05-03 | Stopword-only query does not award false-positive score 1.0 | E | `tests/test_rag_repository.py::test_retrieve_stopword_only_query_falls_back` |
| 05 | 05-03 | Grounded-RAG escalation emits structured `grounded_rag_skipped` log with reason | E | `tests/test_answerers_grounded_rag.py::test_weak_retrieval_falls_through` (and seven peer reasons) |
| 06 | 06-02 | Extract → approve → retrievable | H | `tests/e2e/test_e2e_epic06_knowledge_pipeline.py::test_epic06_extract_approve_then_retrievable` |
| 06 | 06-02 | Reject not retrievable, status filter shows rejected | H | `tests/e2e/test_e2e_epic06_knowledge_pipeline.py::test_epic06_extract_reject_path_not_retrievable` |
| 06 | 06-02 | Approve with edited text publishes edited version | H | `tests/e2e/test_e2e_epic06_knowledge_pipeline.py::test_epic06_approve_with_edited_text_publishes_edited_version` |
| 06 | 06-01 | Second extract pass enqueues zero | H | `tests/e2e/test_e2e_epic06_knowledge_pipeline.py::test_epic06_extract_idempotent_second_pass_enqueues_zero` |
| 06 | 06-02 | Double-approve returns 409 | E | `tests/e2e/test_e2e_epic06_knowledge_pipeline.py::test_epic06_double_approve_returns_409` |
| 06 | 06-02 | Reindex failure → 500 + incident | E | `tests/e2e/test_e2e_epic06_knowledge_pipeline.py::test_epic06_reindex_failure_emits_incident` |
| 07 | 07-01 | Backup run → list → restore round-trip via API | H | `tests/e2e/test_e2e_epic07_backup_restore.py::test_epic07_backup_run_then_restore` |
| 08 | 08-01 | `/suggest` writes a queryable `answer_trace` row with retrieval, routing, guardrail, grounding | H | `tests/e2e/test_e2e_epic08_answer_trace.py::test_epic08_suggest_writes_queryable_trace` |
| 08 | 08-02 (partial) | Static admin shell HTTP 200 | H | `tests/e2e/test_e2e_epic08_web_ui_smoke.py::test_epic08_admin_shell_reachable` |
| 08 | 08-02 (partial) | Static alerts shell HTTP 200 | H | `tests/e2e/test_e2e_epic08_web_ui_smoke.py::test_epic08_alerts_shell_reachable` |
| 08 | 08-02 | `/suggest` persists trace; admin trace list + detail render sources/policy/routing/confidence | H | `tests/e2e/test_e2e_epic08_trace_ui.py::test_epic08_trace_visible_in_web_ui` |
| 08 | 08-03 | NL knowledge op: preview → confirm → reindexed; audit log captured | H | `tests/e2e/test_e2e_epic08_nl_ops.py::test_epic08_nl_op_preview_confirm_reindex` |
| 08 | 08-04 | Trace → correction (moderation branch) → approve → retrievable; audit captured | H | `tests/e2e/test_e2e_epic08_correction_loop.py::test_epic08_trace_correction_to_moderation_then_approved_retrievable` |
| 09 | 09-01 | Operator KB intent detection (slash + Russian free-text with lemma fallback) | H | `tests/test_kb_intent.py` |
| 09 | 09-01 | Telegram attachment normalization (document, photo, audio, video, voice, media_group_id) | H | `tests/test_telegram_update_attachments.py` |
| 09 | 09-01 | Telegram file download two-step with size cap and stream-abort cleanup | H/E | `tests/test_telegram_file_download.py` |
| 09 | 09-02 | Local extractors for PDF/DOCX/PPTX/TXT/image (tesseract patched) + soft_wrap | H | `tests/test_operator_uploads_extractors.py` |
| 09 | 09-03 | Audio/video transcription with duration cap + binary SHA-256 streaming | H/E | `tests/test_operator_uploads_extractors.py` |
| 09 | 09-04 | `/knowledge/operator_upload` auto-approval, dedup short-circuit, confidential propagation | H/E | `tests/test_api_operator_upload.py` |
| 09 | 09-04 | Schema migration idempotency on knowledge_moderation_candidates | H | `tests/test_knowledge_moderation_repository_operator.py` |
| 09 | 09-04 | `is_confidential` round-trip through `RagRepository` | H | `tests/test_rag_repository_confidential.py` |
| 09 | 09-04 | Confidential chunk metadata redacted in `GroundedRagAnswerer` while LLM still receives raw text | H | `tests/test_grounded_rag_confidential_redaction.py` |
| 09 | 09-05 | Bot orchestration: webhook → ack → background upload → summary DM | H/E | `tests/test_bot_gateway_kb_command.py` |
| 09 | 09-05 | Scripted live demo: inline → file → dedup → confidential | H | `scripts/epic09_signoff_demo.sh` |
| 09 | 09-06 | `extract_pdf` falls back to OCR for image-only / vector-glyph PDFs (pypdfium2 render → tesseract); page-cap raises `pdf_too_many_pages_for_ocr` | H/E | `tests/test_operator_uploads_extractors.py::test_extract_pdf_falls_back_to_ocr_for_image_only_pdf`, `::test_extract_pdf_ocr_respects_page_cap`, `::test_extract_pdf_uses_pypdf_when_text_present`, `::test_extract_pdf_returns_empty_when_ocr_yields_nothing` |
| 09 | 09-06 | `ApiError` carries the API's `detail` through `_post`/`_get`/`_patch`/`fetch_file_inspect`/`search_files` while remaining catch-compatible with `httpx.HTTPStatusError` | H/E | `tests/test_bot_gateway_api_client.py::test_post_raises_api_error_with_detail_when_json_body`, `::test_post_api_error_detail_is_none_when_body_not_json`, `::test_post_api_error_detail_is_none_when_detail_field_missing`, `::test_post_api_error_stringifies_non_string_detail`, `::test_get_raises_api_error_with_detail`, `::test_patch_raises_api_error_with_detail`, `::test_fetch_file_inspect_raises_api_error_on_non_404`, `::test_search_files_raises_api_error`, `::test_find_operator_by_username_returns_none_on_404`, `::test_find_operator_by_username_reraises_non_404` |
| 09 | 09-06 | Bot DM surfaces the API's `detail` (Russian friendly mapping) for both attachment and inline-text failure paths | H | `tests/test_bot_gateway_kb_command.py::test_kb_api_error_with_detail_is_surfaced_in_dm`, `::test_kb_inline_api_error_with_detail_is_surfaced`, `::test_kb_friendly_failure_reason_helper_covers_branches` |
| 09 | 09-07 | `OperatorFilesAdminWriter` cascades a single-file delete across `operator_files` + `knowledge_moderation_candidates` + `rag_chunks` + disk binary in one transaction, scoped to operator's own row (admin sees all) | H/E | `tests/test_operator_files_admin.py::test_delete_full_cascade_for_operator_own_file`, `::test_delete_returns_none_for_other_owner_when_operator`, `::test_delete_for_admin_succeeds_on_other_owner`, `::test_delete_unlink_failure_recorded_in_summary`, `::test_delete_cascade_rolls_back_on_failure` |
| 09 | 09-07 | `DELETE /admin/files/{short_id}` and `DELETE /admin/files?confirm=true` enforce scope via cookie session or internal-token + `as_user`; admin's bulk delete stays own-scoped | H/E | `tests/test_api_admin_files_delete.py::test_delete_single_operator_own_file`, `::test_delete_single_operator_other_owner_returns_404`, `::test_delete_single_admin_can_delete_others_file`, `::test_delete_all_operator_scopes_to_self`, `::test_delete_all_admin_scopes_to_own_username`, `::test_delete_all_requires_confirm` |
| 09 | 09-07 | `/file_delete` and `/files_delete_all` use a stateless `confirm` token in a second message; warning + count first, destructive call second | H/E | `tests/test_bot_gateway_file_library.py::test_file_delete_without_confirm_emits_warning`, `::test_file_delete_with_confirm_invokes_api_and_dms_summary`, `::test_file_delete_confirm_token_case_insensitive`, `::test_file_delete_from_non_operator_non_admin_ignored`, `::test_files_delete_all_without_confirm_with_files`, `::test_files_delete_all_with_confirm_invokes_api` |
| 11 | 11-08 | Connect-as-enable via OAuth callback (Google mocked) + configure a service; admin `/calendar_off` keeps token; re-enable = re-run `/connect_calendar`; operator-vs-admin permission split (admin cannot disconnect, operator can) | H/E | `tests/e2e/test_e2e_epic11_enable_and_configure.py::test_epic11_enable_configure_and_permission_split` |
| 11 | 11-07 | Enabled+connected (Google mocked) availability: free time → Russian "available"; busy time → "not available"; single window-wide freeBusy call | H | `tests/e2e/test_e2e_epic11_availability.py::test_e2e_available_slot_returns_russian_available`, `::test_e2e_busy_slot_returns_not_available`, `::test_e2e_token_provider_window_is_single_freebusy_call` |
| 11 | 11-07 | Ambiguous service → one clarifying turn THEN escalate (FR-22); provider error → escalate routed to calendar operator with no fabricated answer | E | `tests/e2e/test_e2e_epic11_availability.py::test_e2e_ambiguous_service_clarifies_then_escalates`, `::test_e2e_provider_error_escalates_to_calendar_operator` |
| 11 | 11-07 | Disabled-project regression: availability-shaped question behaves identically to today (RAG / HITL) with only the cheap gate hit — no token/freeBusy work | H/E | `tests/e2e/test_e2e_epic11_disabled_noop.py::test_e2e_disabled_project_availability_question_grounds_via_rag`, `::test_e2e_disabled_project_availability_question_escalates_when_no_rag` |
| 11 | 11-07 | Expired/revoked token on next use → reconnect_needed + token deleted + incident + operator DM + customer escalates (deferred from 11-04) | E | `tests/e2e/test_e2e_epic11_token_expiry.py::test_e2e_expired_token_reconnect_incident_and_customer_escalates` |
| 11 | 11-07 | `CalendarAvailabilityAnswerer`: disabled→handled=False (cheap gate only); non-scheduling→handled=False; resolved+available/busy/out-of-rules; NoMatch/Ambiguous/no-time→one clarify then escalate; not-connected / reconnect / provider failure → escalate to calendar operator | H/E | `tests/test_calendar_availability_answerer.py` (17 cases) |
| 11 | 11-07 | Pipeline placement: `calendar_availability` runs before `grounded_rag`; disabled project falls through to RAG unchanged | H | `tests/test_calendar_pipeline_placement.py::test_main_pipeline_places_calendar_before_grounded_rag`, `::test_disabled_project_falls_through_to_rag_unchanged` |
| 11 | 11-07 | Calendar wiring helpers + calendar-escalation HITL path (coalesce onto active ticket; route to active calendar operator else primary) | H/E | `tests/test_main_calendar_wiring.py` (12 cases); one-turn clarify state: `tests/test_calendar_clarify_state_repository.py` |
| 11 | 11-07 | Scripted live demo: disabled→HITL (no calendar), enable+configure, enabled-but-not-connected→escalate (no 500 / no fabricated answer) | H/E | `scripts/epic11_signoff.sh` |
| 12 | 12-01 | Sales DB schema + repositories bootstrap idempotently; state/services/followup tables created on first open | H | `tests/test_sales_state_repository.py`; `tests/test_sales_services_repository.py`; `tests/test_sales_followup_queue_repository_extended.py` |
| 12 | 12-02 | Service add/list/delete + sales state endpoints via internal token | H/E | `tests/test_api_sales_services_endpoint.py`; `tests/test_api_sales_state_endpoint.py` |
| 12 | 12-02b | Bot gateway sales NL service-management dispatch + command parsing | H | `tests/test_bot_gateway_sales_commands.py`; `tests/test_bot_gateway_sales_command_dispatch.py` |
| 12 | 12-03 | SalesPersonaAnswerer greeting + scoping + always-on activation gate | H/E | `tests/test_sales_persona_answerer_gate.py`; `tests/test_sales_persona_answerer_greeting.py`; `tests/test_sales_persona_answerer_scoping.py` |
| 12 | 12-04 | KB-first pricing turn; hit quotes verbatim with source_chunk_id; miss escalates with `price_unknown`; drift escalates instead of delivering | H/E | `tests/test_sales_persona_answerer_pricing_hit.py`; `tests/test_sales_persona_answerer_pricing_miss.py`; `tests/test_sales_persona_answerer_pricing_quote_drift.py`; `tests/test_sales_price_lookup.py`; `tests/test_sales_pricing_kb_learning_loop.py` |
| 12 | 12-05 | Autonomous client materials dispatch after scoping completes | H | `tests/test_sales_client_materials_repository.py` |
| 12 | 12-05b | KB upload auto-promotes to client materials via analyzer | H | `tests/test_api_sales_analyze_kb_file_endpoint.py` |
| 12 | 12-06 | Mid-funnel asides keep funnel state (catalog ask, concept ask + RAG fallback, concept op-desc, unknown service) | H/E | `tests/test_sales_concept_keeps_funnel_state.py`; `tests/test_sales_persona_answerer_catalog_ask.py`; `tests/test_sales_persona_answerer_concept_ask_operator_description.py`; `tests/test_sales_persona_answerer_concept_ask_rag_fallback.py`; `tests/test_sales_persona_answerer_concept_ask_unknown_service.py` |
| 12 | 12-07 | Date proposal turn: rendered slot, acceptance → closing, calendar-disabled / drift / no-date-hint paths | H/E | `tests/test_sales_persona_answerer_proposing_hit.py`; `tests/test_sales_persona_answerer_acceptance.py`; `tests/test_sales_persona_answerer_proposing_calendar_disabled.py`; `tests/test_sales_persona_answerer_proposing_drift.py`; `tests/test_sales_proposing_round_trip.py` |
| 12 | 12-08 | Proactive +24h follow-up: enqueue, cancel-on-customer-reply, scheduler fire, stale-skip | H/E | `tests/test_sales_persona_answerer_enqueues_followup.py`; `tests/test_sales_followup_cancel_hook.py`; `tests/test_sales_followup_fire_handler.py`; `tests/test_sales_followup_queue_repository_extended.py` |
| 12 | 12-09 | Pipeline order: `sales_persona` immediately precedes `calendar_availability`; precedes `grounded_rag` | H | `tests/test_pipeline_order_includes_sales_before_calendar.py` |
| 12 | 12-09 | Always-on activation regression: empty `services` catalog + sales-intent message still engages sales (no `count_active` gate) | H | `tests/test_pipeline_sales_active_on_empty_services.py` |
| 12 | 12-09 | Silent-on-non-intent regression: non-sales message → no LLM call, no DB write, no catalog lookup | E | `tests/test_pipeline_sales_silent_on_non_intent.py` |
| 12 | 12-09 | Persona prompt files checked in + non-empty (greeting, scoping, pricing_hit, proposal, followup, catalog, concept_rag, kb_material_analyzer) | H | `tests/test_sales_system_prompts_present.py` |
| 12 | 12-09 | Dev-only `POST /sales/_dev/tick-followup-now` gated by `Settings.app_env == "dev"` (404 in non-dev; 200 in dev) | H/E | `tests/test_sales_dev_tick_endpoint_gated.py` |
| 12 | 12-09 | LLM transport error during sales greeting/scoping falls through to pipeline (no crash) | E | `tests/test_sales_persona_answerer_llm_schema_violation.py::test_llm_transport_error_falls_through_to_pipeline` |

## CI

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) runs Ruff, the **full** pytest run with coverage, then **`pytest -m e2e`** to ensure the E2E marker subset stays green.

## Linear

Use Linear (or any backlog tool) as a **manual** map of what shipped; test names and this matrix should be updated when stories change.
