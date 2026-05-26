# Story 12.05b — KB-upload → automatic client materials analysis

## Objective
When the operator uploads a file to the knowledge base (existing `/kb_add` / KB-upload flow), automatically run an LLM client-sendability analysis on the extracted text. If the file is suitable as customer-facing material (tour catalog, price list, route description, promotional flyer), register a `client_materials` row that points at the same `local_path` — and append a one-line note to the existing KB-upload acknowledgement. If not suitable, the file stays KB-only and the operator sees no extra message. No prompt, no command, no operator confirmation.

## Scope

### In Scope
- `services/api/app/sales/client_materials_analyzer.py` `ClientMaterialsAnalyzer(*, openrouter, operator_files_view, materials_repo)`:
  - `async def analyze_and_register(*, project_id: int, operator_file_short_id: str, now: datetime) -> AnalysisOutcome`.
  - Reads `extracted_text` and file metadata (`mime_type`, `file_extension`, `byte_size`, `local_path`) from `operator_files_view` (the existing read-only view exposed in `services/api/app/operator_files_view.py`).
  - Truncates `extracted_text` to the first 4000 chars (the analysis only needs the first few pages of a catalog; long PDFs are clipped, not chunked).
  - Calls `OpenRouterClient.complete_json(...)` with `system_prompts/sales_kb_material_analyzer.txt` (Russian; instructs the model to judge whether the document is customer-facing promotional / informational material vs. internal-only / confidential / personal data). Response schema: `{"sendable": bool, "reason": str, "suggested_kind": "video"|"photo"|"pdf"|"document", "suggested_caption": str | null}`.
  - On `sendable=True`: registers a `client_materials` row via `ClientMaterialsRepository.add(..., source_operator_file_id=operator_file_short_id, telegram_file_id=None, kind=resolved_kind, local_path=..., byte_size=..., caption=suggested_caption, tags=[])`. Returns `AnalysisOutcome(registered=True, material_id=..., reason=...)`.
  - On `sendable=False`: returns `AnalysisOutcome(registered=False, material_id=None, reason=...)`.
  - **Skip-if-confidential.** When `operator_files_view` marks the file as `is_confidential=True` (the `/kb_add confidential` flag from Epic 09), short-circuit before the LLM call: `AnalysisOutcome(registered=False, reason="confidential_kb_file")`. Confidential files MUST NEVER be promoted to customer-facing materials.
- Frozen dataclass `AnalysisOutcome(registered: bool, material_id: int | None, reason: str)`.
- **Hard cap of 20 active client materials per project.** Before registering, `ClientMaterialsAnalyzer.analyze_and_register` calls `materials_repo.count_active(project_id=...)`. If the count is >= 20, the analyzer SHORT-CIRCUITS the register step regardless of LLM verdict and returns `AnalysisOutcome(registered=False, material_id=None, reason="materials_cap_reached")`. The LLM analysis still runs (so the project's cost accounting captures the analyzer call; see Epic 14 — Usage Monitoring), but no `client_materials` row is written.
- **Operator ack on cap reached.** The bot_gateway hook, on receiving `AnalysisOutcome(registered=False, reason="materials_cap_reached")`, appends a SECOND line to the existing KB-upload ack: `📎 Слот материалов для клиентов занят (20/20). Удалите неиспользуемые материалы или используйте /material для замены.` This message is shown only when the cap is the reason; other `registered=False` cases stay silent (existing behaviour).
- **Cap is hard, not configurable.** The number 20 is a named constant `CLIENT_MATERIALS_CAP_PER_PROJECT` in `services/api/app/sales/client_materials_constants.py`. No `hitl_runtime_config` knob in this story (rationale: Epic 14's alerting attack analysis treats 20 as the contract; making it configurable invites cost-amplification regressions).
- New api endpoint `POST /sales/materials/analyze-kb-file` (internal, service-token-gated) `{project_id, operator_file_short_id}` → `AnalysisOutcome`-shaped JSON. Called by the bot_gateway KB-upload hook (below). The endpoint is the seam — the bot_gateway never invokes the LLM directly.
- **bot_gateway hook** in the existing `/kb_add` / document-upload acknowledgement flow:
  - Locate the existing code path that emits the "Добавлено в базу знаний" (or similar) acknowledgement after a successful `/kb_add` ingest (see `services/bot_gateway/app/main.py` — the `/kb_add` handler chain).
  - After the existing ack is sent, the handler calls `POST /sales/materials/analyze-kb-file`. On `registered=True`, append a second message (or extend the ack) with: `📎 Добавлен в материалы для клиентов (id=<material_id>).` On `registered=False`, no extra message and no error visible to the operator.
  - Failure of the analyze call (LLM error, api unreachable, timeout) is silent to the operator — KB upload succeeds regardless. Logged as `sales_kb_material_analyze_failed` with `{trace_id, operator_file_short_id, error}`. Never block the KB upload path.
  - Hook fires only on a successful KB ingest (the existing `/kb_add` success branch) — not on errors, not on confidential uploads (the confidential flag is also re-checked server-side as a defense-in-depth, per the skip-if-confidential rule above).
- `system_prompts/sales_kb_material_analyzer.txt` — Russian system prompt. Includes:
  - The persona's framing (the bot is sending to prospective customers, not internal staff).
  - Sendability criteria: tour descriptions, price lists, route maps, equipment galleries, promotional flyers → `sendable=True`. Internal docs, invoices, contracts, employee schedules, anything with PII, confidential pricing strategy → `sendable=False`.
  - The strict JSON-out contract.
  - The `suggested_kind` mapping rule based on the file extension (PDF → `pdf`, jpg/png → `photo`, mp4/mov → `video`, anything else → `document`).
  - A `suggested_caption` instruction: one short Russian sentence, never longer than 120 chars; null if no obvious caption fits.

### Out of Scope
- Retroactive scanning of existing KB files (per epic out-of-scope).
- A command to re-trigger analysis on an existing file — operator can use `/material` (reply mode) as an override if they disagree with a `sendable=False` verdict.
- LLM-based tag suggestion. Tags stay empty on auto-promotion; the operator can rebuild tags via `/material_remove` + manual `/material` re-registration in v1.
- Splitting large PDFs into per-page materials.
- Any client-side preview / thumbnail generation — the existing operator-files extractor already handles thumbnail-worthy formats.
- Making the 20-material cap configurable. A later epic may revisit this if real usage demands per-project tuning; v1 is a hard constant.
- Reclamation logic (auto-deleting old client_materials when the cap is hit). Operator must explicitly remove unused materials via `/material_remove` to free a slot.

## Implementation Notes
- **The analyzer is the ONLY new code touching the KB-upload flow.** The bot_gateway hook is a single call after the existing ack — not a refactor of `/kb_add`.
- **`operator_files_view.py` is read-only from the api perspective** (project-context: cross-DB ATTACH, RO open). The analyzer reads from it; it never writes. Writes to `client_materials` go through `ClientMaterialsRepository`.
- **LLM call is JSON-structured** (mirror story 12.03). Schema-violation → `AnalysisOutcome(registered=False, reason="llm_schema_violation")` and logged. Never propagate the exception to the bot_gateway.
- **The same `OpenRouterClient` singleton** that the rest of the api uses (no new client construction). Time / `now` injected via the endpoint dependency.
- **Skip-if-confidential is enforced twice** — once in the analyzer (defense-in-depth), once in the bot_gateway hook (the `confidential` flag is on the `/kb_add` invocation). Both paths log `sales_kb_material_skipped_confidential` with `{trace_id, operator_file_short_id}`. Never log the file's extracted text.
- **The KB-upload path is the source of truth for `local_path`.** The analyzer does NOT copy or re-store the file — `client_materials.local_path` points at the same path the KB ingest already wrote to. When the operator later deletes the KB file (Epic 09 story 9.07), the analyzer-registered `client_materials` row is **left in place** (soft-link semantics) — the dispatcher will fall back to text if the file is gone. v1 accepts this; a cascading-delete is a follow-up.
- **No retry on LLM error.** A transient analyzer failure means the file stays KB-only this time; the operator can `/material` (reply mode) it manually as a workaround.
- **Auto-promotion respects the data-driven dormancy.** If a project has zero `services` rows, KB-uploaded files are still analyzed — there's no downside to having `client_materials` rows in advance — but the dispatcher (12.05) only fires when the answerer is active, which is gated by `services_repo.count_active(...) > 0`.
- **The 20-material cap is materials-only.** Story 12-05c (services extraction) runs in parallel on the same KB upload but counts against a separate concern (`services` table); it has no cap. Do not unify the two enforcement paths.

## Test Plan
### Unit
- `tests/test_client_materials_analyzer_sendable.py` — fake LLM returns `{"sendable": true, "suggested_kind": "pdf", "suggested_caption": "Каталог туров"}` → analyzer calls `materials_repo.add(...)` with the expected args; returns `AnalysisOutcome(registered=True, material_id=..., reason=...)`.
- `tests/test_client_materials_analyzer_not_sendable.py` — fake LLM returns `{"sendable": false, "reason": "internal invoice"}` → no repo write; `AnalysisOutcome(registered=False, ...)`.
- `tests/test_client_materials_analyzer_confidential.py` — `operator_files_view` returns `is_confidential=True` → analyzer short-circuits before the LLM call (asserted by the mocked LLM having zero invocations); returns `reason="confidential_kb_file"`.
- `tests/test_client_materials_analyzer_schema_violation.py` — LLM returns malformed JSON → `AnalysisOutcome(registered=False, reason="llm_schema_violation")`, logged.
- `tests/test_client_materials_analyzer_long_text_truncation.py` — extracted text > 4000 chars → first 4000 chars passed to the LLM (asserted on the captured prompt).
- `tests/test_api_sales_analyze_kb_file_endpoint.py` — endpoint round-trip; service-token-gated (401 without).
- `tests/test_client_materials_analyzer_cap_reached.py` — `materials_repo.count_active(project_id=...) -> 20` → analyzer SHORT-CIRCUITS register step; LLM call MAY OR MAY NOT have run (test fixture documents both shapes — analyzer impl chooses); returns `AnalysisOutcome(registered=False, reason="materials_cap_reached")`. No `materials_repo.add(...)` call is made.
- `tests/test_client_materials_analyzer_cap_boundary.py` — `count_active=19` allows one more registration; `count_active=20` blocks; `count_active=21` (data drift) also blocks (defensive).

### Integration
- `tests/test_bot_gateway_kb_upload_material_hook.py` — full `/kb_add` happy path with a stubbed `ApiClient` returning `AnalysisOutcome(registered=True, material_id=42)` → the operator receives the existing KB ack followed by `📎 Добавлен в материалы для клиентов (id=42).`. `AnalysisOutcome(registered=False)` → no extra message. Analyze-call exception → no extra message; log `sales_kb_material_analyze_failed`; KB ack still sent (the upload itself is not blocked). An additional case asserts that `AnalysisOutcome(registered=False, reason="materials_cap_reached")` triggers a SECOND ack line with the Russian cap message `📎 Слот материалов для клиентов занят (20/20). ...` — distinct from the silent `not-sendable` case.
- `tests/test_bot_gateway_kb_upload_confidential_skips_material.py` — `/kb_add confidential` → the hook either short-circuits in the bot_gateway OR the analyzer returns `confidential_kb_file`; either way, no `client_materials` row is created.

## Automated E2E verification
- `tests/e2e/test_e2e_epic12_kb_auto_material.py` (`@pytest.mark.e2e`, `@pytest.mark.epic("12")`, `@pytest.mark.story("12-05b")`):
  - Operator `/kb_add` a fixture PDF (`tests/fixtures/sales/tour_catalog.pdf`) → KB-ingested + `client_materials` row created + ack includes the materials line.
  - Operator `/kb_add` a fixture file that the LLM stub flags non-sendable (`internal_invoice.pdf`) → KB-only; no row; no extra message.
  - Operator `/kb_add confidential` a sendable-looking file → KB-confidential; no `client_materials` row (skip-if-confidential).

## Manual Verification
1. Upload the «Туры на Багги23.pdf» file to the bot via `/kb_add`. Expect the KB-ingest ack + a second line `📎 Добавлен в материалы для клиентов (id=N).` Run `/material_list` and confirm the row appears.
2. Upload an internal-looking document (e.g. a payroll spreadsheet PDF). Expect the KB-ingest ack with NO extra materials line.
3. Upload a tour-catalog PDF with `/kb_add confidential`. Expect the KB-ingest ack with NO materials line (confidential files never become customer-facing materials).
4. Verify in the answer-trace logs that the analyzer ran and the verdict was recorded; the file's extracted text MUST NOT appear in any log line.

## Done Criteria
- 100% coverage on `client_materials_analyzer.py`, `POST /sales/materials/analyze-kb-file`, and the bot_gateway KB-upload hook glue.
- `ruff check .` passes; E2E green.
- Confidential KB files never become `client_materials` (asserted by both unit and E2E).
- Analyzer failure is silent to the operator and never blocks KB ingest.
- No new fetch / store helpers — the analyzer points at the existing `operator_files`-managed `local_path`.
- File's extracted text never logged (log-capture assertion in unit test).
- KB-upload ack copy in Russian; one-line append; `📎 Добавлен в материалы для клиентов (id=X).` format.
- 20-material-per-project cap enforced in `ClientMaterialsAnalyzer`; cap-reached path returns `materials_cap_reached` reason and the operator gets the Russian cap message in the ack.
- Cap constant lives in `services/api/app/sales/client_materials_constants.py`; no runtime config exposed.
