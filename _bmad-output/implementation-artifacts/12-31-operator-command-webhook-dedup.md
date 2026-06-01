# Story 12.31: Operator-command webhook dedup (no double-act on a redelivered slow command)

Status: review

## Story

As an **operator using the bot's slash / NL commands**,
I want a **redelivered Telegram webhook to act on my command exactly once**,
so that **a slow command (e.g. an NL-service add that calls the LLM) is not executed twice when Telegram retries the webhook**.

**Problem:** Story 12.24 made the *customer* path idempotent (the gateway's `UNIQUE(source_message_id)` persist dedup + the api's atomic `claim_inbound` on `trace_id = tg-update-{update_id}`). But the operator-command handlers in `telegram_webhook` run **before** the customer-path persist dedup:

- `_handle_file_delete_command` / `_handle_file_inspect_command` / `_handle_operator_file_library_command`
- `_handle_kb_command` / `_handle_kb_session_continuation` / `_handle_operator_media_group_orphan`
- `_handle_admin_hitl_command`, persona / whoami / help
- `handle_calendar_command`, `handle_prompt_command`, `handle_admin_project_command`, `handle_admin_nl_dialog`, `handle_sales_command`, `handle_material_command`
- **`handle_operator_service_nl_message`** — makes a slow OpenRouter call

Telegram redelivers a webhook when the handler returns non-200 or exceeds its ~5s deadline. The NL-service handler's LLM call can blow that deadline, so the redelivery re-runs the handler and **double-acts** (a second service added, a second confirmation DM, a second migration hint, etc.). None of these reach `persist_normalized_message`, so Story 12.24's source-message dedup never sees them.

**Why the naive fixes were ruled out** (see the 12-24 "Duplicate sends" framing):
1. **Moving `persist_normalized_message` above the operator handlers pollutes the transcript.** Persist writes the message with `role="user"` into `semantaix_story1.db`, and that transcript feeds `/knowledge/extract` — so operator-command text (`"/hitl_config @x 123"`, NL-service commands) would leak into knowledge extraction.
2. **A blanket webhook-entry `update_id` claim that reuses Story 12.24's `trace_id` idempotency entangles** with the api-side `claim_inbound` / `answer_traces` layers and their tests.

## Acceptance Criteria

1. **Atomic claim at webhook entry, before any handler.** `telegram_webhook` claims the Telegram `update_id` the moment it has a normalized, processable update — before the first operator/customer handler. The first delivery wins; a redelivery with the same `update_id` is dropped with `{"status": "ignored", "reason": "duplicate_update", "trace_id": ...}` (log `telegram_duplicate_update_ignored_at_entry`) and runs **no** handler.
2. **Operator commands act once.** A slow operator NL-service command that Telegram redelivers runs its handler — including the OpenRouter call and the service-add side effect — exactly once; the redelivery is dropped.
3. **Dedicated, non-transcript store.** The claim lives in its own `webhook_update_claims (update_id PRIMARY KEY, claimed_at)` table in its own DB file (`webhook_dedup_db_path`), never the `messages`/`conversations` transcript — so no operator-command text leaks into `/knowledge/extract`. Independent of Story 12.24's `trace_id` / `answer_traces` / `inbound_claims` layers.
4. **Customer dedup net behavior unchanged.** A redelivered customer message still yields exactly one forward / one ack / one ticket; it now short-circuits earlier (at the entry claim → `duplicate_update`) with the `UNIQUE(source_message_id)` persist gate remaining as a second, finer layer.
5. **Internal replay paths unaffected.** The offline-backlog flush and pending-forward replay re-forward via `_forward_inbound_safe` / `_forward_inbound_with_retry`; they never re-enter `telegram_webhook`, so the entry claim does not touch them.
6. **Gates green.** `ruff` clean; full suite at 100% coverage on `services/`; new tests for the repo, the non-transcript property, and the operator-command dedup pass.

## Tasks / Subtasks

- [x] **Claim repo + table** (AC 1,3) — `services/bot_gateway/app/webhook_dedup.py`: `WebhookUpdateClaimRepository` with a `webhook_update_claims (update_id PRIMARY KEY, claimed_at)` table and `claim(update_id) -> bool` (`INSERT OR IGNORE`; `cursor.rowcount == 1`). Re-runs `init_schema` per claim so a reassigned `db_path` (per-test isolation) still has the table — mirrors `AnswerTraceRepository.claim_inbound`.
- [x] **Dedicated DB setting** (AC 3) — `platform_common/settings.py`: `webhook_dedup_db_path: str = ".data/semantaix_webhook_dedup.db"` (its own file on the persistent `.data/` volume; non-transcript).
- [x] **Wire the claim** (AC 1,2,4,5) — `services/bot_gateway/app/main.py`: module-level `webhook_update_claim_repository` singleton (alongside `pending_forward_outbox`); in `_process_telegram_update`, after `normalize_update` + the `None`-ignore guard and before the first handler, `if not webhook_update_claim_repository.claim(normalized.update_id): return {duplicate_update}`.
- [x] **Per-test isolation** (AC 6) — `tests/conftest.py`: one global autouse fixture points `webhook_update_claim_repository.db_path` at a fresh per-test SQLite file (operator-command helpers reuse fixed `update_id` placeholders; without isolation the claim would dedup across tests / pollute `.data/`). Mirrors how api tests reset `answer_trace_repository.db_path`.
- [x] **Tests** (AC 6) — `tests/test_bot_gateway_webhook_dedup.py` (new): repo wins/loses + restart parity; dedicated/non-transcript store; operator NL command redelivery acts once. Updated the two Story 12.24 customer-dedup assertions to the new `duplicate_update` reason (net behavior unchanged). Gave the calendar-alias / file-library / help helper `update_id`s a per-call counter (Telegram's real contract: each delivery is a distinct update) so two genuinely different operator messages in one test don't collide on the entry claim; `message_id` left unchanged so persist behavior is identical.

## Dev Notes

- **Why a dedicated DB, not the transcript or persistence DB:** the store must be non-transcript (AC 3). A separate table in its own file makes that structural and keeps it cleanly isolatable in tests (one global fixture resets the singleton's `db_path`), without entangling with the persistence transcript or Story 12.24's `answer_traces` / `inbound_claims`.
- **Placement — after `normalize_update`, before the first handler:** keyed on the validated `normalized.update_id` (always an int) and only for processable updates (malformed → 400, callback/edited → ignored, neither claimed). It sits inside `telegram_webhook`'s top-level try/except, so a claim DB error becomes a 200 + failure marker rather than a 500 that Telegram would amplify into retries.
- **Customer path reason change:** a redelivered customer message now returns `reason=duplicate_update` (caught at the entry claim) instead of `duplicate_source_message` (the persist gate). The persist gate is unchanged and remains the second layer; the one-forward / one-ticket guarantee is identical. The Story 12.24 tests were updated to assert the new reason.
- **Known edge (same as 12.24):** a crash AFTER the claim but BEFORE a handler finishes leaves a claimed `update_id` with no completed action; a redelivery dedups to it. Acceptable for the observed slow-but-successful operator turn; operator commands are operator-initiated and re-sendable. Out of scope here.
- **Files:** `services/bot_gateway/app/webhook_dedup.py`, `services/bot_gateway/app/main.py`, `platform_common/settings.py`, `tests/conftest.py`.
- **Reuse:** the api-side `claim_inbound` pattern (atomic `INSERT OR IGNORE`, `rowcount == 1`); the `WebhookUpdateClaimRepository` singleton mirrors `pending_forward_outbox` / `operator_file_repository`.
- **Conventions:** SQLite sync repo; `from __future__ import annotations`; ruff E/F/I line-100; 100% coverage gate.

### Project Structure Notes

- Extends Story 12.24 (idempotent inbound delivery) from the customer path to the operator-command path. The two are complementary layers: 12.24 dedups the customer forward (gateway `source_message_id` + api `trace_id`); 12.31 dedups every webhook delivery on `update_id` before any handler. Independent PR off `origin/main`.

### References

- [Source: _bmad-output/implementation-artifacts/investigations/booking-dialog-bugs-investigation.md#Finding 4 / Deduction 1] — Side finding: "Redelivered **operator** commands aren't deduped (gateway dedup sits after operator handlers, bot_gateway `main.py:2621` vs `2390-2609`). Customer booking path is protected. [Confirmed]" — the basis for this story.
- [Source: services/bot_gateway/app/webhook_dedup.py#WebhookUpdateClaimRepository.claim]
- [Source: services/bot_gateway/app/main.py#_process_telegram_update entry claim]
- [Source: services/api/app/answer_trace.py#claim_inbound] (pattern mirrored)
- [Source: _bmad-output/implementation-artifacts/12-24-idempotent-inbound-delivery.md#Duplicate sends]

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–6).** Added a dedicated, non-transcript `WebhookUpdateClaimRepository` (`webhook_update_claims` table in its own `webhook_dedup_db_path` file) and an atomic `update_id` claim at the top of `telegram_webhook`, before any handler. A redelivered slow operator command (incl. the NL-service OpenRouter call) is now dropped with `reason=duplicate_update` and acts exactly once. The customer-path persist dedup is unchanged and remains a second layer (net one forward / one ticket).
- **TDD:** repo + non-transcript + operator-dedup tests written first and watched fail (`ModuleNotFoundError`, then the redelivery returned `status=ok` — the double-act), then implemented to green.
- **Test isolation:** the operator-command helpers reuse fixed `update_id` placeholders, so a global autouse fixture in `tests/conftest.py` resets the singleton's `db_path` per test (mirrors the api `answer_trace_repository.db_path` reset). Three helpers (calendar-alias, file-library, help) now assign a per-call `update_id` so two genuinely different operator messages in one test no longer collide; `message_id` left fixed so persist behavior is unchanged.
- **Regression check:** the two Story 12.24 customer-dedup tests (`test_bot_gateway_webhook.py`, `test_bot_gateway_operator_reply.py`) keep their one-forward guarantee; only the reason string moved to `duplicate_update` (caught earlier at the entry claim). Updated their assertions + docstrings.
- `ruff` clean; full suite **3104 passed at 100% coverage** (CI parity: `pytest --cov --cov-config=.coveragerc`). `webhook_dedup.py` and the new `main.py` branch are fully covered.

### File List

- `services/bot_gateway/app/webhook_dedup.py` (new — `WebhookUpdateClaimRepository` + `webhook_update_claims` table)
- `services/bot_gateway/app/main.py` (modified — `webhook_update_claim_repository` singleton + entry claim before any handler)
- `platform_common/settings.py` (modified — `webhook_dedup_db_path`)
- `tests/conftest.py` (modified — global autouse per-test isolation of the dedup store)
- `tests/test_bot_gateway_webhook_dedup.py` (new — repo, non-transcript, operator-dedup tests)
- `tests/test_bot_gateway_webhook.py` (modified — `duplicate_update` reason for the redelivered customer message)
- `tests/test_bot_gateway_operator_reply.py` (modified — retry test renamed + `duplicate_update` reason)
- `tests/test_bot_gateway_calendar_service_alias.py` (modified — per-call `update_id` helper)
- `tests/test_bot_gateway_file_library.py` (modified — per-call `update_id` helper)
- `tests/test_bot_gateway_help_command.py` (modified — per-call `update_id` helper)
- `_bmad-output/implementation-artifacts/12-31-operator-command-webhook-dedup.md` (new — this story)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified — add `12-31-operator-command-webhook-dedup: review`)
