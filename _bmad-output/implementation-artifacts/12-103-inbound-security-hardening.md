# Story 12.103: Inbound security hardening — message length cap, per-user rate limiting, prompt-injection mitigation

Status: review

## Story

As the **platform operator**,
I want every customer inbound message to pass through input-length enforcement, per-user rate limiting, and LLM prompt-injection mitigation before it reaches the answer pipeline,
so that **a malicious or careless external user cannot craft a message that overrides the bot's system prompt, nor can they flood the API to exhaust the OpenRouter token budget**.

## Motivation

Three gaps exist today (confirmed via codebase analysis, 2026-06-09):

| Gap | Where | Risk |
|-----|--------|------|
| No per-user rate limit | `bot_gateway/main.py` — customer message path | Token-cost DoS: unlimited messages per user |
| No input length cap | `bot_gateway/main.py` and `api_client.py` | Long crafted inputs inflate LLM prompt cost |
| Customer question concatenated raw into LLM prompt | `openrouter_client.py:187` — `"Question:\n" + question` | Prompt injection: user can append instruction overrides |

The message travels completely unsanitized: Telegram webhook → `normalize_update` → `api_client.forward_inbound(text=text)` → `GroundedRagAnswerer` → `openrouter_client.answer_grounded(question=question)` → `"Question:\n" + question` inserted into the LLM user block.

## Acceptance Criteria

1. **Length cap enforced.** A customer message exceeding `inbound_max_message_chars` (default 1000) is truncated to that limit before forwarding. No rejection or user-visible message — silent truncation preserves the API response flow. Truncation is logged at `INFO` with the original and truncated lengths.
2. **Rate limit enforced.** A customer who sends more than `inbound_rate_limit_messages` (default 10) messages in any `inbound_rate_limit_window_seconds` (default 300) rolling window receives a polite Russian reply ("Слишком много сообщений. Пожалуйста, подождите немного и попробуйте снова.") and the message is not forwarded to the API. Subsequent messages within the same window continue to be rejected.
3. **Operator messages exempt.** Rate limiting and length truncation apply only to customer messages (senders whose `normalized.username` does not match `_effective_operator_username()`). Operator messages are unaffected.
4. **LLM prompt quote-fencing.** In `openrouter_client.answer_grounded` and `openrouter_client.verify_answer`, the customer question is wrapped in `<customer_question>` XML tags so the LLM clearly sees it as user content, not instruction:
   ```
   <customer_question>
   {question}
   </customer_question>
   ```
   This change applies to both the `answer_grounded` and `verify_answer` calls (both use `"Question:\n" + question` today).
5. **Settings configurable.** `inbound_max_message_chars`, `inbound_rate_limit_messages`, `inbound_rate_limit_window_seconds`, and `inbound_rate_limit_db_path` are `Settings` fields with sensible defaults; they appear in `.env.example`.
6. **Rate limit state is persistent across restarts.** The sliding window counter is stored in a dedicated SQLite file (`.data/semantaix_rate_limits.db`), not in-memory, so a bot_gateway restart does not reset counts.
7. **Gates green.** `ruff check .` clean; `pytest --cov` at 100% on `platform_common/` and `services/`; new repository and security path have full branch coverage.

## Tasks / Subtasks

- [x] **Add Settings fields** (AC: 5) — in `platform_common/settings.py`, after line 144 (`inbound_pipeline_timeout_seconds`), add:
  ```python
  # Story 12.103 (INPUT SECURITY) — inbound length cap and per-user rate limiting
  inbound_max_message_chars: int = 1000
  inbound_rate_limit_messages: int = 10
  inbound_rate_limit_window_seconds: int = 300
  inbound_rate_limit_db_path: str = ".data/semantaix_rate_limits.db"
  ```
  Also add these four to `.env.example` under the existing `INBOUND_*` block.

- [x] **Create `InboundRateLimitRepository`** (AC: 2, 6) — new file `services/bot_gateway/app/rate_limit_repository.py`:
  - SQLite table `inbound_request_counts(chat_id INTEGER PRIMARY KEY, window_start_iso TEXT NOT NULL, message_count INTEGER NOT NULL)` — created with `CREATE TABLE IF NOT EXISTS` on `__init__`.
  - `def check_and_record(self, *, chat_id: int, now: datetime, max_messages: int, window_seconds: int) -> bool` — returns `True` if the message is allowed, `False` if rate-limited. Logic: if the stored `window_start` is older than `window_seconds`, reset count to 1 and allow; else if count >= max_messages, deny (do NOT increment); else increment and allow. All in a single `INSERT OR REPLACE` upsert after the decision. Constructor: `def __init__(self, *, db_path: str)` — mirroring existing repo shape.

- [x] **Inject security checks in bot_gateway customer path** (AC: 1, 2, 3) — in `services/bot_gateway/app/main.py`, in the customer-message branch (the path that calls `_forward_inbound_safe` / adds the `_forward_live_and_clear_pending` background task):
  1. **Length truncation** — before forwarding, apply: `text = text[:settings.inbound_max_message_chars]` if `len(text) > settings.inbound_max_message_chars`. Log the truncation at `INFO`.
  2. **Rate limit check** — call `rate_limit_repo.check_and_record(chat_id=..., now=datetime.now(UTC), max_messages=..., window_seconds=...)`. If denied: call `_safe_send_text(chat_id=..., text=_RATE_LIMIT_REPLY)` and return early (do not forward). Log at `WARNING`. If allowed: proceed as normal.
  - Define module-level constant: `_RATE_LIMIT_REPLY = "Слишком много сообщений. Пожалуйста, подождите немного и попробуйте снова."`.
  - Instantiate `InboundRateLimitRepository(db_path=settings.inbound_rate_limit_db_path)` as a module-level singleton, parallel to existing repo singletons.

- [x] **Wrap question in XML tags in openrouter_client** (AC: 4) — in `services/api/app/openrouter_client.py`:
  - Line 187: change `"Question:\n" + question` → `"<customer_question>\n" + question + "\n</customer_question>"`
  - Line 291 (verify_answer user block): same replacement.
  - No other changes to prompt construction.

- [x] **Tests** (AC: 7) — 100% branch coverage on all new and modified code:
  - `tests/test_bot_gateway_rate_limit_repository.py` — unit-test `InboundRateLimitRepository` using `tmp_path` fixture: first request allowed; up to max allowed; max+1 rejected; window expiry resets count; chat_ids isolated.
  - `tests/test_bot_gateway_inbound_security.py` — integration tests using FastAPI `TestClient` on bot_gateway with monkeypatched `InboundRateLimitRepository` and `ApiClient.forward_inbound`:
    - Message exactly at length limit → forwarded as-is; message 1 char over → truncated to limit.
    - Allowed message → forwarded (repo returns True).
    - Rate-limited message → `_RATE_LIMIT_REPLY` sent to customer, NOT forwarded.
    - Operator message → always forwarded, rate-limit repo never called.
  - `tests/test_openrouter_client.py` (modify existing) — assert `answer_grounded` and `verify_answer` payloads contain `<customer_question>` tags around the question; assert they do NOT contain the raw `"Question:\n"` prefix.

## Dev Notes

**Files:**
- CREATE: `services/bot_gateway/app/rate_limit_repository.py`
- UPDATE: `platform_common/settings.py` (4 new fields after line 144)
- UPDATE: `.env.example` (4 new `INBOUND_*` lines under existing block)
- UPDATE: `services/bot_gateway/app/main.py` (security checks in customer path + singleton repo)
- UPDATE: `services/api/app/openrouter_client.py` (lines 187 and 291)
- UPDATE: `tests/test_openrouter_client.py` (assert XML tag wrapping)
- CREATE: `tests/test_bot_gateway_rate_limit_repository.py`
- CREATE: `tests/test_bot_gateway_inbound_security.py`

**Injection site in bot_gateway:** The customer-message forward path is in `_process_telegram_update`. The normalization gives `normalized.text` (the raw string) and `normalized.chat_id`. Operator check happens around line 491 (`normalized.username != operator_username`). The security checks belong in the customer branch, after operator detection, before the background-task add / `_forward_inbound_safe` call.

**Rate limit repo pattern:** Mirror `WebhookUpdateClaimRepository` (services/bot_gateway/app/webhook_dedup.py) — sync SQLite, constructor takes `db_path`, `CREATE TABLE IF NOT EXISTS` in `__init__`. Never use `asyncio.to_thread` in tests — call sync methods directly. In production async code, wrap with `await asyncio.to_thread(rate_limit_repo.check_and_record, ...)` matching the existing repo-from-async pattern.

**XML tag rationale:** XML tags (`<customer_question>`) are the most widely supported prompt-injection boundary across LLM providers (including OpenRouter-hosted models). They do not require model-specific formatting and do not break the existing grounding logic, which looks for `ESCALATE_TO_HUMAN` in the output — not in the input. The system prompts already use structured prose; adding structured input tags is a minimal, non-breaking change.

**Do NOT** add injection-keyword regex detection (blocklists go stale, false-positive on legitimate customer questions like "Ignore my previous reservation — can I reschedule?"). The XML tag boundary + length cap is the correct defense-in-depth layer at this stage.

**Settings field naming follows established conventions:**
- `inbound_*` prefix (matches `inbound_ack_message`, `inbound_interim_delay_seconds`, etc.)
- `*_db_path` suffix for path fields (matches `rag_db_path`, `hitl_ticket_db_path`, etc.)

**Time injection:** Pass `now: datetime` into `check_and_record` so the window boundary test is deterministic (no `datetime.now()` inside the repo — project-wide rule).

**Logging events:**
- `"inbound_message_truncated"` — `INFO`, fields: `trace_id`, `chat_id`, `original_len`, `truncated_len`
- `"inbound_rate_limited"` — `WARNING`, fields: `trace_id`, `chat_id`, `window_start`, `message_count`

### References

- [services/bot_gateway/app/main.py:491] — operator username check (pattern for customer-vs-operator branching)
- [services/bot_gateway/app/api_client.py:79] — `"text": text` field sent to `/conversations/inbound`
- [services/api/app/openrouter_client.py:187] — `"Question:\n" + question` (injection site 1)
- [services/api/app/openrouter_client.py:291] — same pattern in `verify_answer` (injection site 2)
- [platform_common/settings.py:138–144] — existing `inbound_*` settings block (insertion point)
- [services/bot_gateway/app/webhook_dedup.py] — reference shape for a sync SQLite repo in bot_gateway

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Completion Notes List

- Implemented all 4 security layers: settings fields, `InboundRateLimitRepository`, length cap + rate limit in handler, XML tag wrapping in openrouter_client.
- Coverage gap on the truncation if-body (lines inside the `if len(text) > max_chars:` block) was caused by Starlette TestClient running the async ASGI handler in a worker thread that coverage.py doesn't trace in isolation. Resolved by extracting truncation into a synchronous module-level helper `_apply_inbound_length_cap` and adding direct unit tests that call it without TestClient — coverage traces synchronous function calls normally.
- Per-test SQLite isolation for `rate_limit_repo` added to the global conftest autouse fixture (`_isolate_webhook_update_claims`) so rate-limit counts don't leak across tests.
- All 3503 tests pass; `ruff check .` clean; 100% coverage.

### File List

- `platform_common/settings.py` — 4 new `inbound_*` settings fields
- `.env.example` — 4 new `INBOUND_*` env vars
- `services/bot_gateway/app/rate_limit_repository.py` — new `InboundRateLimitRepository`
- `services/bot_gateway/app/main.py` — `_RATE_LIMIT_REPLY` constant, `rate_limit_repo` singleton, `_apply_inbound_length_cap` helper, security checks in customer path
- `services/api/app/openrouter_client.py` — XML tag wrapping in `answer_grounded` and `verify_answer`
- `tests/conftest.py` — `rate_limit_repo.db_path` isolation in global autouse fixture
- `tests/test_bot_gateway_rate_limit_repository.py` — new unit tests for `InboundRateLimitRepository`
- `tests/test_bot_gateway_inbound_security.py` — new integration + unit tests for security path
- `tests/test_openrouter_client.py` — assertions for XML tag wrapping
