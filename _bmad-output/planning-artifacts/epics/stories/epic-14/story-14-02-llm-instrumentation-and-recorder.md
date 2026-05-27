# Story 14.02 — OpenRouter LLM-call instrumentation + `UsageRecorder` seam + `call_outcome` enum

## Objective
Wire OpenRouter calls and answerer-exit points to record per-call usage rows in `usage_llm_calls`. Ship the **polymorphic, async fire-and-forget `UsageRecorder` ingestion seam** that all three trackers (LLM in this story, messages in 14.03, HITL in 14.04) dispatch through. Capture `prompt_tokens`, `completion_tokens`, `cost_usd` (NULL-tolerant) from the OpenRouter response — no self-counting — plus the `call_outcome` enum value reported by the calling answerer. Usage-write failure MUST NOT raise into the user-facing pipeline (NFR-8).

**As an** admin,
**I want** every OpenRouter LLM call attributed to a project with token counts, cost, model name, and a downstream outcome,
**So that** the dashboard, alerts, and `/usage` command can show me where my spend goes — including the wasted-spend slice (`verifier_rejected`, `guardrails_blocked`, `error`) that doesn't reach the customer.

PRD reference: **FR-27** (Polymorphic Ingestion Seam), **FR-28** (OpenRouter LLM Usage Capture), **NFR-8** (Usage-Capture Liveness).

## Scope

### In Scope
- **`UsageRecorder` class** in `services/api/app/usage/recorder.py`:
  - Constructor: `__init__(self, *, llm_repo, message_repo, hitl_repo, queue_maxsize: int = 1024)`.
  - One public signature: `async def record(self, *, tracker_type: str, project_id: int, payload: dict, trace_id: str | None = None) -> None`. Validates `tracker_type ∈ {'llm','messages','hitl'}`. Enqueues onto an internal `asyncio.Queue`. **Returns immediately**; never awaits the actual SQLite write.
  - Background consumer task `_consumer_loop` started on app startup via lifespan hook; drains the queue, dispatches each item to the correct repo via `asyncio.to_thread(repo.record, ...)`. On any exception, emits `usage_record_failed` structured log with `tracker_type`, `trace_id`, `error_type` — **never re-raises**.
  - Graceful shutdown: `aclose()` drains remaining items then cancels the consumer task. Called from app lifespan teardown.
  - Queue overflow policy: when `queue_maxsize` is reached, **drop oldest** (use `queue.get_nowait()` to make room, log `usage_queue_overflow_drop` with the dropped tracker_type). Trades data fidelity for liveness — explicitly per NFR-8.
- **`UsageLlmCallRepository.record(*, project_id, model_name, prompt_tokens, completion_tokens, cost_usd, call_outcome, trace_id, created_at)`** implementation in `services/api/app/usage/repositories.py` (replacing the 14.01 skeleton):
  - Validates `call_outcome` against the `frozenset` `{'customer_visible_answer', 'verifier_rejected', 'escalated_to_hitl', 'guardrails_blocked', 'moderation_triggered', 'error'}`; raises `ValueError` on mismatch.
  - INSERTs one row. `cost_usd` may be `None` (NULL).
  - Idempotency: NOT keyed by an external id; the repo simply inserts. Duplicate writes (e.g. on retry) are allowed and counted as additional rows (recorder is fire-and-forget so retries are not expected here — at-most-once write semantics).
- **OpenRouter client instrumentation** in `services/api/app/openrouter_client.py` (or its successor):
  - After every successful OpenRouter response, capture `usage.prompt_tokens`, `usage.completion_tokens`, `usage.cost` (may be missing), and the model field. **No self-counting via tokenizers.**
  - Build a payload `{model_name, prompt_tokens, completion_tokens, cost_usd, call_outcome, trace_id, created_at}` where `call_outcome` defaults to the value the caller passed via a new `call_outcome` parameter on the client method (typed `Literal['customer_visible_answer', ...]`); created_at is an injected clock's now (UTC ISO-8601 with `Z` suffix).
  - Call `await recorder.record(tracker_type='llm', project_id=ctx.project_id, payload=payload, trace_id=ctx.trace_id)`. The recorder is constructor-injected into the client (or accessed via a module-level singleton bound at app startup — match the existing pattern for shared collaborators).
  - On OpenRouter network/5xx failure, the existing failure handling is unchanged — but ALSO record a row with `call_outcome='error'` and `prompt_tokens=0`, `completion_tokens=0`, `cost_usd=None` (so the error rate is visible). This is the ONLY use of `call_outcome='error'` in this story (full error-monitoring is out of scope per brainstorm A5).
- **`call_outcome` propagation through `GroundedRagAnswerer`** in `services/api/app/answerers/grounded_rag.py`:
  - Successful customer-visible answer → caller passes `call_outcome='customer_visible_answer'` on the OpenRouter call that produced it.
  - Verifier-rejected path → caller passes `call_outcome='verifier_rejected'` on the verifier LLM call (the grounded LLM that produced the candidate keeps `customer_visible_answer` if it would otherwise have been one — match the actual call that incurred each cost; verifier is its own row).
  - Guardrails-blocked path → caller passes `call_outcome='guardrails_blocked'` on the LLM call whose output was rejected.
  - HITL escalation from `GroundedRagAnswerer` (no answer produced) → the FINAL LLM call that decided to escalate carries `call_outcome='escalated_to_hitl'`.
- **`call_outcome='moderation_triggered'`** in `services/api/app/answerers/client_materials_analyzer.py` (Epic 12 module — or wherever KB-upload auto-analysis lives) — when an OpenRouter call originates from `/kb_add` upload processing (not customer traffic).
- **Outcome-source convention** (load-bearing for FR-31 wasted-spend math): each LLM call carries the outcome that BEST describes ITS contribution; the verifier-rejected case produces TWO rows: the grounded LLM call with `customer_visible_answer` (it produced what would have been a customer answer, but the verifier rejected it — that grounded call's cost still counts as "wasted") — **but** brainstorm decision says wasted-spend = `verifier_rejected | guardrails_blocked | error`. **Resolution:** the grounded LLM call in the verifier-rejected path is tagged `verifier_rejected` (not `customer_visible_answer`) — the answerer KNOWS at write time whether the verifier accepted it. This story implements that ordering: grounded LLM call → buffer → verifier call → if verifier rejected, both rows tagged `verifier_rejected`; if accepted, both rows tagged `customer_visible_answer`. The wasted-spend math (14.05) follows the `call_outcome` value, not the call position.
- **Lifespan integration** — `UsageRecorder` instance created at app startup, consumer task launched as `asyncio.create_task`; `await recorder.aclose()` called at teardown. Wire through the existing FastAPI lifespan hook.

### Out of Scope
- Message-volume instrumentation (14.03 owns it; this story does ship the `UsageMessageRepository.record` implementation as a stub that 14.03 fleshes out — actually NO, 14.03 implements its own repo method; this story leaves 14.03's repo at `NotImplementedError`).
- HITL event instrumentation (14.04).
- Daily roll-up worker / retention purge (14.05).
- Web UI dashboard / API endpoints / `/usage` bot command (14.06–14.08).
- Alerting (14.09).
- Updating the backup runbook (14.10).

## Implementation Notes
- **Async fire-and-forget pattern** — `UsageRecorder.record` is `async` only because it does `await self._queue.put(...)`; it never awaits the actual write. The consumer task runs in a single background coroutine — there is no per-write task creation (which would lose ordering and explode memory under load).
- **`asyncio.Queue` sizing** — default `queue_maxsize=1024`. Empirically more than one second of headroom at 100k LLM calls/day (per NFR-10). Configurable via `Settings.usage_queue_maxsize` env var.
- **Drop-oldest overflow** — the recorder is a fire-hose, not a journal. NFR-8 says LLM call integrity > usage-write durability; under sustained load the dashboard sees a flat-valley gap, not a system stall.
- **No retries inside the consumer** — a failed write logs `usage_record_failed` and moves on. Retrying would invite duplicate rows + queue stalls. The cost of silent loss is bounded by the brainstorm K2 decision.
- **No `await` on the SQLite call inside the user-facing path** — `openrouter_client` calls `recorder.record(...)` (just an enqueue) and proceeds immediately. The actual `to_thread(repo.record, ...)` runs on the consumer task — never blocks the inbound.
- **Injected clock** — recorder accepts `clock: Callable[[], datetime]` for `created_at` so tests can freeze time. Default to `lambda: datetime.now(timezone.utc)`.
- **Injected recorder** — the OpenRouter client gets the recorder via constructor (matches existing collaborator-injection pattern). Tests use `AsyncMock`-able `record` to assert call shape.
- **`call_outcome='error'` row construction** — the OpenRouter client catches `httpx.HTTPStatusError` / `httpx.RequestError` after the normal logging path, schedules the `error` row write, and re-raises the original exception. The user-facing pipeline already handles the raise.
- **`project_id` propagation** — every LLM call site must have `ctx.project_id` from `AnswerContext` (Epic 08+). The OpenRouter client method gains a required `project_id: int` parameter; existing call sites pass it through. Compile-time enforced (no `project_id=None` fallbacks).
- **Structured logging** — `usage_record_failed`, `usage_queue_overflow_drop` (with dropped `tracker_type`), `usage_recorder_started`, `usage_recorder_stopped`. Never log `cost_usd` to operator-scope log routes (Epic 14 leaves all instrumentation log routes admin-scope-equivalent; operator-visible log surfaces are a future epic concern).
- **Performance** — typical add to inbound critical path: < 50 µs (one `queue.put_nowait`). Measured by a microbenchmark in `tests/test_usage_recorder_overhead.py`.

## Test Plan

### Unit
- `tests/test_usage_recorder.py`:
  - `record(tracker_type='llm', ...)` enqueues without awaiting the write; `record` returns within microseconds even when the consumer is paused.
  - Consumer drains the queue and dispatches to the LLM repo with the right kwargs (verified with a `MagicMock(spec=UsageLlmCallRepository)`).
  - Consumer-loop exception path: the repo `record` raises → consumer logs `usage_record_failed` with `error_type` → loop continues (next item is processed).
  - Queue overflow: fill queue to `maxsize` while consumer is paused → next `record` triggers drop-oldest + `usage_queue_overflow_drop` log; the newest item lands in the queue; the oldest is gone.
  - `aclose()`: queued items are drained, consumer task is cancelled cleanly; calling `record` after `aclose` raises `RuntimeError`.
  - `record` with `tracker_type='bogus'` raises `ValueError`.
  - Frozen clock: `created_at` matches the injected clock's return value.
- `tests/test_usage_llm_call_repository.py`:
  - `record` inserts a row with all fields including NULL `cost_usd`; round-trip read returns the same values.
  - `record` with `call_outcome='bogus'` raises `ValueError` BEFORE touching the DB.
  - All six valid `call_outcome` values insert successfully.
- `tests/test_openrouter_client_instrumentation.py`:
  - Mock OpenRouter response with `usage.prompt_tokens=10, usage.completion_tokens=20, usage.cost=0.0034` → recorder receives a payload with those values + `cost_usd=0.0034`.
  - Mock OpenRouter response with `usage.prompt_tokens=10, usage.completion_tokens=20` (no `cost`) → recorder receives `cost_usd=None`.
  - Mock OpenRouter 5xx failure → recorder receives ONE payload with `call_outcome='error', prompt_tokens=0, completion_tokens=0, cost_usd=None`; the underlying exception re-raises into the caller (unchanged).
  - `model_name` captured per-row from the response (not from project config).
- `tests/test_grounded_rag_call_outcome_propagation.py`:
  - Successful pipeline → recorder sees `call_outcome='customer_visible_answer'`.
  - Verifier rejects → recorder sees TWO rows with `call_outcome='verifier_rejected'` (the grounded LLM call's row + the verifier's row both tagged).
  - Guardrails block → recorder sees `call_outcome='guardrails_blocked'` on the final LLM call.
  - No answerer handled / escalated to HITL from `GroundedRagAnswerer` → recorder sees `call_outcome='escalated_to_hitl'` on the final LLM call.
- `tests/test_client_materials_analyzer_call_outcome.py`:
  - KB-upload analysis call → recorder sees `call_outcome='moderation_triggered'`.

### Contract
- N/A — no new api endpoints; existing OpenRouter contract unchanged.

### Integration
- `tests/test_usage_recorder_lifespan.py` — boot the api app via `TestClient`; assert `usage_recorder_started` log appears, a synthetic LLM call lands a row in `usage_llm_calls`, `usage_recorder_stopped` log appears on teardown.

## Automated E2E verification
- `tests/e2e/test_e2e_epic14_llm_round_trip.py` (`@pytest.mark.e2e @pytest.mark.epic("14") @pytest.mark.story("14-02")`):
  - Stub OpenRouter responses (mock `httpx.AsyncClient.post` per the existing E2E harness pattern).
  - Send a customer message via `POST /conversations/inbound` that reaches the grounded answerer → assert one row in `usage_llm_calls` with `call_outcome='customer_visible_answer'` and matching `prompt_tokens`/`completion_tokens`/`cost_usd`/`model_name`.
  - Send a customer message whose verifier rejects → assert rows tagged `call_outcome='verifier_rejected'`.
  - Send a customer message that escalates → assert the final LLM call's row carries `call_outcome='escalated_to_hitl'`.
  - Force OpenRouter 5xx → assert one row with `call_outcome='error'`; assert the answer pipeline emits the expected HITL escalation downstream (unchanged behavior).

## Manual Verification
1. `docker compose up --build -d`; send a Telegram customer message that the grounded answerer answers successfully → `sqlite3 .data/semantaix_usage.db "SELECT * FROM usage_llm_calls ORDER BY id DESC LIMIT 5;"` shows the new row with non-NULL tokens, populated `cost_usd` (if OpenRouter returned one), correct `model_name`, and `call_outcome='customer_visible_answer'`.
2. Force a verifier rejection (e.g. via a test prompt that the verifier flags) → rows show `call_outcome='verifier_rejected'`.
3. Force the OpenRouter client to fail (set an invalid API key for one call) → row with `call_outcome='error'`; bot's customer-facing flow still produces an escalation ack (no 500).
4. Block writes by temporarily chmod-locking `semantaix_usage.db` → `usage_record_failed` log appears, but customer messages continue to be answered (NFR-8).

## Done Criteria
- 100% line coverage on `services/api/app/usage/recorder.py`, `services/api/app/usage/repositories.py` (the LLM repo method, plus the existing skeleton coverage), the OpenRouter-client instrumentation diff, and the `call_outcome` propagation diffs in `grounded_rag.py` + `client_materials_analyzer.py`.
- `ruff check .` passes.
- All six `call_outcome` enum values exercise an insert path in tests.
- Usage-write failure injection does not raise into the OpenRouter client caller (NFR-8 verified by test).
- Async fire-and-forget verified — the inbound critical path completes BEFORE the consumer task processes the queued item (frozen-clock + paused-consumer test).
- E2E LLM round-trip green.
