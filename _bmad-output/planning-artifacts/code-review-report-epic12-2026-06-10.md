# Epic 12 Code Review Report

**Date:** 2026-06-10  
**Baseline:** commit `30f01d4` (immediately before story 12.19)  
**Target:** HEAD  
**Scope:** 164 files, 18,653 insertions (+), 601 deletions (−)  
**Groups reviewed:** 5 parallel agents

---

## Triage Summary

| Severity | Count | Disposition |
|----------|-------|-------------|
| 🔴 HIGH  | 12    | Must-Fix — new stories or immediate hotfix |
| 🟠 MEDIUM | 10   | Nice-to-Fix — backlog candidates |
| 🟡 LOW   | 8     | Nice-to-Fix — backlog or inline |

---

## MUST-FIX

### M-01 — `asyncio.CancelledError` escapes `except Exception` in pipeline timeout
**File:** `services/api/app/main.py`  
**Severity:** 🔴 HIGH  
Python 3.11: `asyncio.CancelledError` inherits from `BaseException`, not `Exception`. The pipeline timeout block catches `Exception`, so a cancellation escapes the handler, leaves the inbound claim locked, and silences the customer permanently (no ack, no HITL ticket).  
**Fix:** Add `except asyncio.CancelledError: raise` before `except Exception`, or change to `except BaseException` with explicit re-raise for non-Exception types.

---

### M-02 — Lambda captures `request` by reference in `asyncio.to_thread`
**File:** `services/api/app/main.py`  
**Severity:** 🔴 HIGH  
A closure passed to `asyncio.to_thread` captures a mutable loop variable or function-scope reference rather than a snapshot value. This creates a race condition where the wrong request object can be processed.  
**Fix:** Capture the value explicitly in a default argument: `lambda req=request: …`.

---

### M-03 — `_handle_cancellation` sends static string to HITL instead of customer message
**File:** `services/api/app/sales/sales_persona_answerer.py`  
**Severity:** 🔴 HIGH — DATA LOSS  
When a cancellation is detected, the HITL ticket is created with `CANCELLATION_ESCALATION_CONTEXT` (a static template string) instead of the customer's verbatim message. The operator receives no context about what the customer actually said.  
**Fix:** Pass `ctx.customer_message` (or equivalent) as the ticket body alongside the context template.

---

### M-04 — `_handle_human_request` same data-loss bug
**File:** `services/api/app/sales/sales_persona_answerer.py`  
**Severity:** 🔴 HIGH — DATA LOSS  
Identical issue to M-03: operator receives static text instead of the customer's actual message when the customer explicitly asks for a human.  
**Fix:** Same pattern as M-03.

---

### M-05 — Rate limit SELECT + INSERT not atomic — race condition
**File:** `services/bot_gateway/app/rate_limit_repository.py`  
**Severity:** 🔴 HIGH  
`check_and_record()` reads the current window count in one statement and writes in a second statement. Under concurrent callers (e.g. two simultaneous Telegram webhook deliveries), both reads see count < limit and both proceed, bypassing the limit.  
**Fix:** Wrap in `BEGIN EXCLUSIVE` transaction, or replace the two statements with a single atomic UPSERT + CASE expression.

---

### M-06 — Rate limit window boundary off-by-one (`>` should be `>=`)
**File:** `services/bot_gateway/app/rate_limit_repository.py`  
**Severity:** 🔴 HIGH  
Window boundary comparison uses `>` instead of `>=`, allowing one extra message per window than configured.  
**Fix:** Change boundary check to `>=`.

---

### M-07 — Non-breaking spaces (U+00A0) from Telegram break `_BEFORE_RE` time parsing
**File:** `services/api/app/service_resolver.py`  
**Severity:** 🔴 HIGH  
Telegram clients sometimes substitute U+00A0 (non-breaking space) for regular spaces. Python's default `\s` in compiled regex does not match U+00A0. Messages containing NBSP around time tokens silently fail to parse, causing the service resolver to return wrong or empty results.  
**Fix:** Add `re.UNICODE` flag (already default in Python 3 but harmless to add) and pre-normalize ` ` → ` ` in the input string before regex matching.

---

### M-08 — English-locale test uses Russian context (`_ctx()` instead of `_en_ctx()`)
**File:** `tests/test_sales_persona_answerer_awaiting_time.py:120`  
**Severity:** 🔴 HIGH — TEST CORRECTNESS  
A test that is supposed to exercise the English (`en`) locale path creates its context with the Russian default `_ctx()`. The English code path is never actually exercised; the test gives false confidence.  
**Fix:** Replace `_ctx()` with `_en_ctx()` in the affected test(s).

---

### M-09 — Disjunctive assertions mask regressions in sales persona tests
**File:** `tests/test_sales_persona_answerer_early_busy_check.py` (line ~4013)  
**Severity:** 🔴 HIGH — TEST CORRECTNESS  
Multiple tests use `assert A or B or C` where A, B, C are string substring checks. This passes even if only one wrong branch fires. A regression in the specific branch under test cannot be detected.  
**Fix:** Replace with `assert specific_expected_string in reply` for each relevant case; or parametrize the test.

---

### M-10 — Ambient clock (`datetime.now(UTC)`) in rate limit repo tests — non-deterministic
**File:** `tests/test_bot_gateway_rate_limit_repository.py:900`  
**Severity:** 🔴 HIGH — TEST RELIABILITY  
`_now()` is a live clock call. Tests that derive expected window boundaries from `old_now = _now(); … new_now = _now()` are non-deterministic across slow CI runners. The exact window boundary (exactly 300 s) is never tested, leaving the `>` vs `>=` comparison (M-06 above) completely uncovered.  
**Fix:** Inject a fixed sentinel `datetime`. Add `test_message_exactly_at_window_boundary_is_rejected`.

---

### M-11 — Webhook dedup concurrent race not tested
**File:** `tests/test_bot_gateway_webhook_dedup.py`  
**Severity:** 🔴 HIGH — TEST GAP  
Only sequential `claim()` calls are tested. The actual concurrency guarantee (that `INSERT OR IGNORE` prevents double-processing under simultaneous arrivals) has no test. If the implementation regressed to a plain `INSERT`, both concurrent callers would succeed and a message would be processed twice.  
**Fix:** Add a test that spawns two threads calling `repo.claim(same_update_id)` concurrently and asserts exactly one returns `True`.

---

### M-12 — `_is_configured()` private method declared in Protocol
**File:** `services/api/app/llm_model_health.py`  
**Severity:** 🔴 HIGH — DESIGN BUG  
A `Protocol` class declares `_is_configured()` as part of the interface. Python Protocols do not enforce name-mangled / private method matching across classes; an implementor that names the method differently will silently satisfy the structural check while the runtime call fails.  
**Fix:** Rename to `is_configured()` (public) in the Protocol and all implementors.

---

## NICE-TO-FIX

### N-01 — Raw `question` concatenated into LLM system prompt (prompt injection surface)
**File:** `services/api/app/sales/sales_persona_answerer.py` — `_build_pricing_hit_reply`  
**Severity:** 🟠 MEDIUM  
Customer-supplied question text is appended directly to the LLM system prompt without escaping. A crafted message containing `</system>` or role-switching instructions could influence model behavior.  
**Fix:** Place customer input only in the `user` turn, never the system prompt. If system-prompt inclusion is needed, wrap with XML-safe delimiters and truncate to a safe length.

---

### N-02 — Hardcoded `"Europe/Moscow"` in `_format_today_ru`
**File:** `services/api/app/sales/sales_persona_answerer.py`  
**Severity:** 🟠 MEDIUM  
Multi-tenant: an operator in a different timezone gets the wrong weekday in LLM prompts, producing incorrect "today is Tuesday" style context.  
**Fix:** Use `ZoneInfo(ctx.timezone)` (same pattern used elsewhere in the answerer).

---

### N-03 — `_timeonly_counteroffer_start` derives tz from `now.tzinfo` not `ctx.timezone`
**File:** `services/api/app/sales/sales_persona_answerer.py`  
**Severity:** 🟠 MEDIUM  
Inconsistent with all other calendar operations in the same file, which use `ZoneInfo(ctx.timezone)`.  
**Fix:** Replace `now.tzinfo` with `ZoneInfo(ctx.timezone)`.

---

### N-04 — Rate limit bypassed via offline-backlog flush path
**File:** `services/bot_gateway/app/rate_limit_repository.py`  
**Severity:** 🟠 MEDIUM  
Messages queued while Telegram was offline and delivered in a burst upon reconnection bypass the per-window rate limit (backlog flush path does not check the repository).  
**Fix:** Route backlog flush through the same `check_and_record` gate, or document the explicit decision to exempt backlog delivery.

---

### N-05 — `object()` used as LLM stub instead of asserting-stub
**File:** `tests/test_sales_persona_answerer_pitching_reask_guard.py:5243`  
**Severity:** 🟠 MEDIUM  
A test that should assert "LLM was not called" uses a bare `object()` as the stub. If the code path accidentally calls the LLM, the test raises `AttributeError` rather than a clear assertion failure, making the failure mode opaque.  
**Fix:** Use `MagicMock(spec=…)` with `assert_not_called()` so failures are explicit.

---

### N-06 — `assert freebusy.calls >= 1` instead of `== 1`
**File:** `tests/test_sales_persona_answerer_early_busy_check.py:3118`  
**Severity:** 🟠 MEDIUM  
Masks duplicate calendar queries: if the answerer makes 3 freebusy calls instead of 1, the test still passes.  
**Fix:** Change to `assert freebusy.call_count == 1`.

---

### N-07 — `TelegramNotifier.notify` failure paths not tested
**File:** `tests/test_telegram_notifier.py`  
**Severity:** 🟠 MEDIUM  
No test covers `send_message` raising `httpx.HTTPStatusError` (401/403) or a network error. If these propagate, they crash the incident pipeline.  
**Fix:** Add `test_notify_send_fails_logs_and_does_not_raise` and `test_notify_network_error_does_not_raise`.

---

### N-08 — `_wire()` omits `sales_followup_repository.db_path` isolation
**File:** `tests/test_inbound_claim_no_silence_on_setup_failure.py:2239`  
**Severity:** 🟠 MEDIUM  
`_wire()` sets db paths for five repositories but not `sales_followup_repository`, which is directly exercised in one of the tests. On a developer machine with `.data/` from a live run, this can corrupt or read production data.  
**Fix:** Add `sales_followup_repository.db_path = str(tmp_path / "sales_followup.sqlite3")` to `_wire()`.

---

### N-09 — XML delimiter injection via customer message in LLM prompt
**File:** `services/api/app/openrouter_client.py`  
**Severity:** 🟡 LOW  
`</customer_question>` in a customer message terminates the XML tag boundary used to delimit user input in the prompt template, potentially allowing context escape.  
**Fix:** Either HTML-escape `<` and `>` in user input before XML embedding, or switch to a role-based prompt structure that doesn't use XML delimiters for user content.

---

### N-10 — `_isolate_webhook_update_claims` skips if `bot_gateway.app.main` not yet imported
**File:** `tests/conftest.py:37`  
**Severity:** 🟡 LOW  
The autouse isolation fixture is a no-op for the very first import of the bot_gateway module, leaving a narrow window where the real `.data/` DB is used.  
**Fix:** Replace the `sys.modules.get(…)` guard with `importlib.import_module(…)` to force the module import before isolation.

---

### N-11 — `normalizer.lemmas()` called twice per item in price lookup loop
**File:** `services/api/app/sales/price_lookup.py`  
**Severity:** 🟡 LOW  
Minor CPU waste; cache the result in a local variable.

---

### N-12 — Missing blank lines before `_BOOKING_COMMIT_RE` → ruff E302
**File:** `services/api/app/sales/sales_persona_answerer.py`  
**Severity:** 🟡 LOW  
Will fail `ruff check` if the constant is added at module scope. Add two blank lines before the constant definition.

---

### N-13 — Spurious `# pragma: no cover` on reachable method
**File:** `tests/test_sales_persona_answerer_closing_restart.py`  
**Severity:** 🟡 LOW  
The pragma suppresses coverage on a branch that is actually reachable. Remove it and ensure the branch is exercised by tests.

---

### N-14 — Scheduler Dockerfile copies entire `services/` tree
**Severity:** 🟡 LOW  
Copies all service directories into the scheduler image unnecessarily, bloating the image and coupling unrelated services.  
**Fix:** Scope the `COPY` to only `services/scheduler/` and shared platform code.

---

### N-15 — `webhook_dedup` table grows unbounded
**File:** `services/bot_gateway/app/` (dedup repository)  
**Severity:** 🟡 LOW  
No TTL or cleanup job. On a high-volume deployment this will grow indefinitely.  
**Fix:** Add a periodic `DELETE FROM webhook_dedup WHERE claimed_at < (unixepoch() - 86400)` purge, or a `CHECK` constraint via a triggered cleanup.

---

### N-16 — `_init_schema()` called on every `check_and_record()` (hot path overhead)
**File:** `services/bot_gateway/app/rate_limit_repository.py`  
**Severity:** 🟡 LOW  
Schema init is idempotent but runs a DDL statement on every call. Move to a `__init__` or class-level once-init guard.

---

## Recommended Follow-Up Stories

The Must-Fix items above span two categories:

**Immediate hotfix (should not wait for a sprint):**
- M-01 `asyncio.CancelledError` escape (production customer-silencing bug)
- M-03/M-04 HITL escalation data-loss (operators receiving no context)

**New backlog stories (batch into a bug-fix story or next sprint):**
- M-02, M-05, M-06, M-07 — runtime correctness
- M-08 through M-12 — test suite integrity

---

## Epic 12 Closure Note

Stories 12-19 and 12-21 through 12-103 (83 stories) are closed as **done** — all code is merged to main via PRs #142–#148.

Two stories remain outstanding:
- **12-09** (`pipeline-wiring-and-e2e-signoff`): `in-progress`
- **12-20** (`broaden-russian-date-time-parsing-ordinals-and-bare-hours`): `ready-for-dev`

Epic 12 will be fully closed when these two are complete.
