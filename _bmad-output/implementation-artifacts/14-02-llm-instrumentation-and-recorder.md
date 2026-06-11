# Story 14.02: OpenRouter LLM-call instrumentation + `UsageRecorder` seam + `call_outcome` enum

Status: review

## Story

As an admin,
I want every OpenRouter LLM call attributed to a project with token counts, cost, model name, and a downstream outcome,
so that the dashboard, alerts, and `/usage` command can show me where my spend goes — including the wasted-spend slice (`verifier_rejected`, `guardrails_blocked`, `error`) that doesn't reach the customer.

## Acceptance Criteria

1. `services/api/app/usage/recorder.py` exports `UsageRecorder` with:
   - Constructor: `__init__(self, *, llm_repo, message_repo, hitl_repo, queue_maxsize: int = 1024)`
   - `async def record(self, *, tracker_type: str, project_id: int, payload: dict, trace_id: str | None = None) -> None` — validates `tracker_type ∈ {'llm','messages','hitl'}`, enqueues, returns immediately
   - `_consumer_loop` background task drains queue, dispatches to correct repo via `asyncio.to_thread`; on exception logs `usage_record_failed` with `tracker_type`, `trace_id`, `error_type` — never re-raises
   - Drop-oldest overflow: when queue is full, drop the oldest item, log `usage_queue_overflow_drop` with the dropped `tracker_type`, then enqueue the new item
   - `async def aclose(self) -> None` — drains remaining items then cancels consumer task; calling `record()` after `aclose()` raises `RuntimeError`
   - Structured logs: `usage_recorder_started`, `usage_recorder_stopped`

2. `UsageLlmCallRepository.record(row: UsageLlmCallRow) -> None` is implemented in `services/api/app/usage/repositories.py` (replacing the 14.01 skeleton):
   - Validates `row.call_outcome` against `CALL_OUTCOMES` frozenset; raises `ValueError` on mismatch BEFORE touching the DB
   - INSERTs one row into `usage_llm_calls`; `cost_usd` may be `None`

3. `CALL_OUTCOMES = frozenset({'customer_visible_answer', 'verifier_rejected', 'escalated_to_hitl', 'guardrails_blocked', 'moderation_triggered', 'error'})` is a module-level constant in `repositories.py`

4. `services/api/app/openrouter_client.py` is instrumented:
   - New `@dataclass(frozen=True) class LlmUsageCapture(model_name: str, prompt_tokens: int, completion_tokens: int, cost_usd: float | None)` in the same file
   - `_chat` now returns `tuple[str, LlmUsageCapture]` — the capture is always populated after a successful response; `prompt_tokens`/`completion_tokens` default to 0 if absent in the OpenRouter response
   - On HTTP failure (`httpx.HTTPStatusError` / `httpx.RequestError`): if `recorder` + `project_id` are set, fire-and-forget an `error` row BEFORE re-raising
   - `answer_grounded` returns `tuple[str, LlmUsageCapture]`
   - `verify_grounding` returns `tuple[GroundingVerdict, LlmUsageCapture]`
   - `complete_json` and `summarize_offerings` write their own usage row immediately (outcome known at call time — see Dev Notes)
   - `OpenRouterClient.__init__` gains `recorder: UsageRecorder | None = None` and `clock: Callable[[], datetime] | None = None` parameters; defaults to `None` / `lambda: datetime.now(timezone.utc)`

5. `GroundedRagAnswerer.try_answer` propagates `call_outcome`:
   - After calling `answer_grounded` (which returns `(text, grounded_capture)`), buffers `grounded_capture` in a local variable
   - After calling `verify_grounding` (which returns `(verdict, verifier_capture)`), buffers `verifier_capture`
   - Outcome `customer_visible_answer`: both rows written with `call_outcome='customer_visible_answer'`
   - Outcome `verifier_rejected` (verdict.label != "GROUNDED"): both rows written with `call_outcome='verifier_rejected'`
   - Outcome `guardrails_blocked` / `profanity_detected`: grounded + verifier rows written with `call_outcome='guardrails_blocked'`
   - LLM sentinel path (answer == `ESCALATE_TO_HUMAN`): grounded row written with `call_outcome='escalated_to_hitl'`; no verifier row
   - LLM error / verifier error exception paths: grounded row written with `call_outcome='error'`; recorder also fires from inside `_chat` on the HTTP error path (see AC 4); caller guard avoids double-write for the same call
   - `GroundedRagAnswerer.__init__` gains `recorder: UsageRecorder | None = None` and `clock: Callable[[], datetime] | None = None` parameters

6. `ClientMaterialsAnalyzer.analyze_and_register` in `services/api/app/sales/client_materials_analyzer.py`:
   - After the successful `complete_json` call returns (i.e., the LLM call succeeds), the row is written with `call_outcome='moderation_triggered'` by the `_OpenRouterClient.complete_json` path
   - `ClientMaterialsAnalyzer.__init__` needs no recorder parameter — the recorder is owned by `OpenRouterClient` (which the analyzer already holds as `self._openrouter`)
   - The `complete_json` method on `OpenRouterClient` takes new params `project_id: int | None = None`, `call_outcome: str = 'moderation_triggered'`, `trace_id: str | None = None`
   - `ClientMaterialsAnalyzer.analyze_and_register` passes `project_id` through to `complete_json`

7. `platform_common/settings.py` has `usage_queue_maxsize: int = 1024` (field after `usage_db_path`)

8. `services/api/app/main.py`:
   - Imports `UsageRecorder` from `services.api.app.usage.recorder`
   - Creates `usage_recorder = UsageRecorder(llm_repo=UsageLlmCallRepository(db_path=settings.usage_db_path), message_repo=UsageMessageRepository(db_path=settings.usage_db_path), hitl_repo=UsageHitlEventRepository(db_path=settings.usage_db_path), queue_maxsize=settings.usage_queue_maxsize)` after the usage DB bootstrap
   - `openrouter_client` is re-created (or patched) to include `recorder=usage_recorder`
   - Two `@app.on_event("startup")` / `@app.on_event("shutdown")` hooks start/stop the consumer task
   - OR: uses `asyncio.create_task` in the startup hook and `aclose()` in the shutdown hook
   - `GroundedRagAnswerer` is wired with `recorder=usage_recorder`

9. `USAGE_QUEUE_MAXSIZE=1024` is added to `.env.example`

10. 100% line coverage on `services/api/app/usage/recorder.py`, the modified sections of `repositories.py`, the instrumented diff of `openrouter_client.py`, the outcome-propagation diffs in `grounded_rag.py` and `client_materials_analyzer.py`
11. `ruff check .` clean; all 6 `call_outcome` enum values have insert paths in tests

## Tasks / Subtasks

- [x] Task 1 — `UsageRecorder` + queue infrastructure (AC: 1)
  - [x] 1.1 Create `services/api/app/usage/recorder.py`
  - [x] 1.2 Implement `record()` enqueue (validates tracker_type, raises RuntimeError if closed)
  - [x] 1.3 Implement `_consumer_loop` with drop-oldest overflow and `usage_record_failed` logging
  - [x] 1.4 Implement `aclose()` drain + cancel
  - [x] 1.5 Emit `usage_recorder_started` / `usage_recorder_stopped` structured logs

- [x] Task 2 — `UsageLlmCallRepository.record` implementation (AC: 2, 3)
  - [x] 2.1 Add `CALL_OUTCOMES` frozenset constant to `repositories.py`
  - [x] 2.2 Replace `raise NotImplementedError` in `UsageLlmCallRepository.record` with real INSERT
  - [x] 2.3 Validate `row.call_outcome` before INSERT; raise `ValueError` on mismatch

- [x] Task 3 — `OpenRouterClient` instrumentation (AC: 4)
  - [x] 3.1 Add `LlmUsageCapture` dataclass to `openrouter_client.py`
  - [x] 3.2 Update `_chat` to return `tuple[str, LlmUsageCapture]`; capture usage from response
  - [x] 3.3 Add `recorder`, `clock` params to `OpenRouterClient.__init__`
  - [x] 3.4 In `_chat` error path: fire-and-forget `error` row if recorder + project_id available
  - [x] 3.5 Update `answer_grounded` to return `tuple[str, LlmUsageCapture]`
  - [x] 3.6 Update `verify_grounding` to return `tuple[GroundingVerdict, LlmUsageCapture]`
  - [x] 3.7 Update `complete_json` and `summarize_offerings` to write usage row immediately
  - [x] 3.8 Add `project_id`, `call_outcome`, `trace_id` params to `complete_json` and `summarize_offerings`

- [x] Task 4 — `GroundedRagAnswerer` call_outcome propagation (AC: 5)
  - [x] 4.1 Add `recorder`, `clock` params to `GroundedRagAnswerer.__init__`
  - [x] 4.2 Buffer `grounded_capture` from `answer_grounded` return value
  - [x] 4.3 Buffer `verifier_capture` from `verify_grounding` return value
  - [x] 4.4 Write both rows after outcome is known (all 5 outcome branches)

- [x] Task 5 — `ClientMaterialsAnalyzer` `moderation_triggered` (AC: 6)
  - [x] 5.1 Pass `project_id`, `call_outcome='moderation_triggered'`, `trace_id=None` to `complete_json`

- [x] Task 6 — Settings + `.env.example` (AC: 7, 9)
  - [x] 6.1 Add `usage_queue_maxsize: int = 1024` to `platform_common/settings.py`
  - [x] 6.2 Add `USAGE_QUEUE_MAXSIZE=1024` to `.env.example`

- [x] Task 7 — Wire `UsageRecorder` into `main.py` (AC: 8)
  - [x] 7.1 Import `UsageRecorder` and repo classes for usage
  - [x] 7.2 Create `usage_recorder` singleton after `bootstrap_usage_db` call
  - [x] 7.3 Recreate `openrouter_client` with `recorder=usage_recorder`
  - [x] 7.4 Add startup hook to launch `usage_recorder._consumer_loop` as `asyncio.create_task`
  - [x] 7.5 Add shutdown hook to `await usage_recorder.aclose()`
  - [x] 7.6 Pass `recorder=usage_recorder` to `GroundedRagAnswerer` constructor

- [x] Task 8 — Tests (AC: 10, 11)
  - [x] 8.1 `tests/test_usage_recorder.py` — all recorder unit tests (see test plan)
  - [x] 8.2 `tests/test_usage_llm_call_repository.py` — repo INSERT + validation tests
  - [x] 8.3 `tests/test_openrouter_client_instrumentation.py` — usage capture + error row tests
  - [x] 8.4 `tests/test_grounded_rag_call_outcome_propagation.py` — all 5 outcome branches
  - [x] 8.5 `tests/test_client_materials_analyzer_call_outcome.py` — moderation_triggered
  - [x] 8.6 `tests/test_usage_recorder_lifespan.py` — source-inspection wiring tests
  - [x] 8.7 `tests/e2e/test_e2e_epic14_llm_round_trip.py` — E2E (mocked httpx)

## Dev Notes

### New file: `services/api/app/usage/recorder.py`

```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.api.app.usage.repositories import (
        UsageHitlEventRepository,
        UsageLlmCallRepository,
        UsageMessageRepository,
    )

_LOG = logging.getLogger(__name__)
_VALID_TRACKER_TYPES: frozenset[str] = frozenset({"llm", "messages", "hitl"})


class UsageRecorder:
    def __init__(
        self,
        *,
        llm_repo: "UsageLlmCallRepository",
        message_repo: "UsageMessageRepository",
        hitl_repo: "UsageHitlEventRepository",
        queue_maxsize: int = 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._llm_repo = llm_repo
        self._message_repo = message_repo
        self._hitl_repo = hitl_repo
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_maxsize)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._consumer_task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        """Start the background consumer. Call once from startup hook."""
        self._consumer_task = asyncio.create_task(self._consumer_loop())
        _LOG.info("usage_recorder_started")

    async def record(
        self,
        *,
        tracker_type: str,
        project_id: int,
        payload: dict,
        trace_id: str | None = None,
    ) -> None:
        if self._closed:
            raise RuntimeError("UsageRecorder is closed")
        if tracker_type not in _VALID_TRACKER_TYPES:
            raise ValueError(f"Unknown tracker_type: {tracker_type!r}")
        item = {"tracker_type": tracker_type, "project_id": project_id,
                "payload": payload, "trace_id": trace_id}
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            else:
                _LOG.warning(
                    "usage_queue_overflow_drop",
                    extra={"dropped_tracker_type": dropped["tracker_type"]},
                )
        await self._queue.put(item)

    async def _consumer_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._dispatch(item)
            except Exception as exc:
                _LOG.warning(
                    "usage_record_failed",
                    extra={
                        "tracker_type": item.get("tracker_type"),
                        "trace_id": item.get("trace_id"),
                        "error_type": type(exc).__name__,
                    },
                )
            finally:
                self._queue.task_done()

    async def _dispatch(self, item: dict) -> None:
        tracker_type = item["tracker_type"]
        payload = item["payload"]
        if tracker_type == "llm":
            row = _build_llm_row(project_id=item["project_id"], payload=payload)
            await asyncio.to_thread(self._llm_repo.record, row)
        elif tracker_type == "messages":
            row = _build_message_row(project_id=item["project_id"], payload=payload)
            await asyncio.to_thread(self._message_repo.record, row)
        elif tracker_type == "hitl":
            row = _build_hitl_row(project_id=item["project_id"], payload=payload)
            await asyncio.to_thread(self._hitl_repo.record, row)

    async def aclose(self) -> None:
        self._closed = True
        await self._queue.join()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        _LOG.info("usage_recorder_stopped")
```

The `_build_llm_row`, `_build_message_row`, `_build_hitl_row` helpers construct the typed row
dataclasses from the raw payload dicts. These are thin wrappers; put them in the same file.

### Modified file: `services/api/app/usage/repositories.py`

Add at module level (before the class definitions):

```python
CALL_OUTCOMES: frozenset[str] = frozenset({
    "customer_visible_answer",
    "verifier_rejected",
    "escalated_to_hitl",
    "guardrails_blocked",
    "moderation_triggered",
    "error",
})
```

Replace `UsageLlmCallRepository.record`:

```python
def record(self, row: UsageLlmCallRow) -> None:
    if row.call_outcome not in CALL_OUTCOMES:
        raise ValueError(f"Invalid call_outcome: {row.call_outcome!r}")
    with sqlite3.connect(self._db_path) as conn:
        conn.execute(
            "INSERT INTO usage_llm_calls"
            " (project_id, model_name, prompt_tokens, completion_tokens,"
            "  cost_usd, call_outcome, trace_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.project_id, row.model_name, row.prompt_tokens,
                row.completion_tokens, row.cost_usd, row.call_outcome,
                row.trace_id, row.created_at,
            ),
        )
```

Note: `id` is `INTEGER PRIMARY KEY` so it's auto-assigned; do NOT include it in the INSERT.
The `UsageMessageRepository.record` and `UsageHitlEventRepository.record` remain
`raise NotImplementedError` — stories 14.03 and 14.04 implement those.

### Modified file: `services/api/app/openrouter_client.py`

**Add `LlmUsageCapture` dataclass** near the top (after existing imports):

```python
from datetime import datetime, timezone
from collections.abc import Callable

@dataclass(frozen=True)
class LlmUsageCapture:
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    created_at: str  # UTC ISO-8601 with Z
```

**Update `OpenRouterClient.__init__`** — add params (keep existing logic, don't break tests):

```python
def __init__(
    self,
    recorder: "UsageRecorder | None" = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    settings = get_settings()
    self.api_key = settings.openrouter_api_key
    ...
    self._recorder = recorder
    self._clock = clock or (lambda: datetime.now(timezone.utc))
```

**Update `_chat`** to return `tuple[str, LlmUsageCapture]` and add params:

```python
async def _chat(
    self,
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float | None = None,
    project_id: int | None = None,
    call_outcome: str | None = None,
    trace_id: str | None = None,
) -> tuple[str, LlmUsageCapture]:
    ...
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(...)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        if self._recorder is not None and project_id is not None:
            now_str = self._clock().strftime("%Y-%m-%dT%H:%M:%SZ")
            asyncio.ensure_future(
                self._recorder.record(
                    tracker_type="llm",
                    project_id=project_id,
                    payload={
                        "model_name": model,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "cost_usd": None,
                        "call_outcome": "error",
                        "trace_id": trace_id,
                        "created_at": now_str,
                    },
                    trace_id=trace_id,
                )
            )
        raise

    usage = data.get("usage") or {}
    capture = LlmUsageCapture(
        model_name=data.get("model", model),
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
        cost_usd=usage.get("cost"),
        created_at=self._clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    text = data["choices"][0]["message"]["content"]
    return text, capture
```

**Update `answer_grounded`** to return `tuple[str, LlmUsageCapture]`:
Just propagate the `(text, capture)` tuple returned by `_chat`. Add `project_id`, `trace_id` params for error recording; do NOT pass `call_outcome` here (caller decides outcome after verifier).

**Update `verify_grounding`** to return `tuple[GroundingVerdict, LlmUsageCapture]`:
Unwrap the text from `_chat`, parse verdict, return `(verdict, capture)`. Add `project_id`, `trace_id` params.

**Update `complete_json`**: After a successful response (after `_chat`), if `recorder` + `project_id` are set, fire-and-forget an LLM row. The method already knows the `call_outcome` (passed in). Example call from `ClientMaterialsAnalyzer`:
```python
result, capture = await self._chat(model=..., messages=..., ...)
# fire-and-forget if recorder wired
if self._recorder is not None and project_id is not None:
    asyncio.ensure_future(self._recorder.record(
        tracker_type="llm", project_id=project_id,
        payload={"model_name": capture.model_name, ...},
    ))
```

Add `project_id: int | None = None`, `call_outcome: str = "moderation_triggered"`, `trace_id: str | None = None` params to `complete_json` and `summarize_offerings`.

**`_parse_verdict` stays unchanged** (module-level function — no diff needed).

### Modified file: `services/api/app/answerers/grounded_rag.py`

**Update `GroundedRagAnswerer.__init__`** — add params:
```python
def __init__(
    self,
    *,
    ...,
    recorder: "UsageRecorder | None" = None,
    clock: Callable[[], datetime] | None = None,
) -> None:
    ...
    self._recorder = recorder
    self._clock = clock or (lambda: datetime.now(timezone.utc))
```

**Update `try_answer`** — buffer captures and write rows with final outcome:

```python
# Replace the answer_grounded call:
try:
    answer, grounded_capture = await self._llm.answer_grounded(
        ...,
        project_id=ctx.project_id,
        trace_id=ctx.trace_id,
    )
except Exception as exc:
    # error row already fired from inside _chat; no double-write here
    return self._skip(reason="llm_generator_error", ...)

# If LLM returns sentinel — write grounded row as escalated_to_hitl
if is_sentinel:
    self._record_llm_row(grounded_capture, ctx, "escalated_to_hitl")
    return self._skip(reason="escalate_sentinel", ...)

# Replace the verify_grounding call:
try:
    verdict, verifier_capture = await self._llm.verify_grounding(
        ...,
        project_id=ctx.project_id,
        trace_id=ctx.trace_id,
    )
except Exception as exc:
    self._record_llm_row(grounded_capture, ctx, "error")
    return self._skip(reason="verifier_error", ...)

if verdict.label != "GROUNDED":
    self._record_llm_row(grounded_capture, ctx, "verifier_rejected")
    self._record_llm_row(verifier_capture, ctx, "verifier_rejected")
    return self._skip(reason="verifier_not_grounded", ...)

if not decision.valid:
    self._record_llm_row(grounded_capture, ctx, "guardrails_blocked")
    self._record_llm_row(verifier_capture, ctx, "guardrails_blocked")
    return self._skip(reason="guardrail_invalid", ...)

if contains_profanity:
    self._record_llm_row(grounded_capture, ctx, "guardrails_blocked")
    self._record_llm_row(verifier_capture, ctx, "guardrails_blocked")
    return self._skip(reason="profanity_detected", ...)

# Success path
self._record_llm_row(grounded_capture, ctx, "customer_visible_answer")
self._record_llm_row(verifier_capture, ctx, "customer_visible_answer")
return AnswerResult(handled=True, ...)
```

Add private helper:
```python
def _record_llm_row(
    self, capture: LlmUsageCapture, ctx: AnswerContext, call_outcome: str
) -> None:
    if self._recorder is None or ctx.project_id is None:
        return
    asyncio.ensure_future(
        self._recorder.record(
            tracker_type="llm",
            project_id=ctx.project_id,
            payload={
                "model_name": capture.model_name,
                "prompt_tokens": capture.prompt_tokens,
                "completion_tokens": capture.completion_tokens,
                "cost_usd": capture.cost_usd,
                "call_outcome": call_outcome,
                "trace_id": ctx.trace_id,
                "created_at": capture.created_at,
            },
            trace_id=ctx.trace_id,
        )
    )
```

Import `LlmUsageCapture` from `services.api.app.openrouter_client` at the top.

**IMPORTANT — `answer_grounded` and `verify_grounding` now return tuples**: All existing tests
that call these methods and expect a plain string/verdict must be updated to unpack the tuple
(e.g., `answer, _ = await llm.answer_grounded(...)`). Audit `tests/` for all call sites before
writing new tests.

### Modified file: `services/api/app/sales/client_materials_analyzer.py`

The `_OpenRouterClient` Protocol declares `complete_json`; it needs to add the new params:

```python
class _OpenRouterClient(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
        project_id: int | None = None,
        call_outcome: str = "moderation_triggered",
        trace_id: str | None = None,
    ) -> dict[str, Any]: ...
```

In `analyze_and_register`, update the `complete_json` call:

```python
payload = await self._openrouter.complete_json(
    system=system,
    user=user,
    project_id=project_id,
    call_outcome="moderation_triggered",
    trace_id=None,
)
```

### Modified file: `services/api/app/main.py`

After the existing `bootstrap_usage_db(settings.usage_db_path)` line (line ~511), add:

```python
from services.api.app.usage.recorder import UsageRecorder
from services.api.app.usage.repositories import (
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
```

(Add to the import block at the top — keep ruff import order: `services.*` imports are alphabetical.)

After the `bootstrap_usage_db` call:

```python
# Epic 14 story 14.02: fire-and-forget usage recorder seam
usage_recorder = UsageRecorder(
    llm_repo=UsageLlmCallRepository(db_path=settings.usage_db_path),
    message_repo=UsageMessageRepository(db_path=settings.usage_db_path),
    hitl_repo=UsageHitlEventRepository(db_path=settings.usage_db_path),
    queue_maxsize=settings.usage_queue_maxsize,
)
```

Change the `openrouter_client` construction (line ~219) to inject the recorder. Since the recorder
is created AFTER `openrouter_client` today, there are two options:

**Option A (recommended)**: Move `openrouter_client` construction to AFTER `usage_recorder`:
- The recorder is created at line ~512 (right after `bootstrap_usage_db`)
- Move `openrouter_client = OpenRouterClient()` from line ~219 to line ~515 and add `recorder=usage_recorder`
- This shifts all module-level singletons that depend on `openrouter_client` (they're all at line ~500+)
  so there's no ordering problem — check there are no references to `openrouter_client` before line ~500

**Option B**: Add `recorder=None` default in `OpenRouterClient.__init__` and call `openrouter_client._recorder = usage_recorder` after the recorder is created. This avoids the reorder but is less clean.

Go with **Option A**. Verify there are no forward references.

Add startup hook (after existing `@app.on_event("startup")` hooks):

```python
@app.on_event("startup")
async def start_usage_recorder() -> None:
    usage_recorder.start()
```

Add shutdown hook:

```python
@app.on_event("shutdown")
async def stop_usage_recorder() -> None:
    await usage_recorder.aclose()
```

Wire `GroundedRagAnswerer` with `recorder=usage_recorder` (find its constructor call in main.py, ~line 640+, add the kwarg).

### `asyncio.ensure_future` vs `asyncio.create_task`

Use `asyncio.ensure_future(coro)` (not `create_task`) inside synchronous methods that fire
coroutines onto the running event loop. Inside `async def` methods, `asyncio.create_task(coro)`
is preferred. Both are fire-and-forget. In the `_record_llm_row` helper (which is called from
async context), use `asyncio.create_task`. In error paths inside `_chat` where a coroutine
is fired before re-raising, use `asyncio.ensure_future`.

### Test: existing call sites that must be updated

Before writing new tests, update ALL existing tests that call:
- `openrouter_client.answer_grounded(...)` — now returns `(str, LlmUsageCapture)`; unpack with `text, _ = await ...`
- `openrouter_client.verify_grounding(...)` — now returns `(GroundingVerdict, LlmUsageCapture)`; unpack with `verdict, _ = await ...`

Run `grep -rn "answer_grounded\|verify_grounding" tests/` to find all affected tests.

### Key tests to write

`tests/test_usage_recorder.py`:
```python
async def test_record_enqueues_without_awaiting_write():
    # pause consumer, call record(), verify it returns immediately
    # then let consumer run, verify repo.record was called

async def test_consumer_dispatches_to_llm_repo():
    # mock UsageLlmCallRepository; send tracker_type='llm'; verify repo.record called with right row

async def test_consumer_error_logs_and_continues():
    # repo.record raises; consumer logs usage_record_failed; next item still processed

async def test_drop_oldest_on_overflow():
    # fill queue to maxsize with consumer paused
    # call record() → oldest dropped, usage_queue_overflow_drop logged, newest in queue

async def test_aclose_drains_then_stops():
    # enqueue items, call aclose(), verify all items processed, consumer cancelled

async def test_record_raises_after_aclose():
    await recorder.aclose()
    with pytest.raises(RuntimeError):
        await recorder.record(tracker_type="llm", ...)

async def test_record_raises_on_invalid_tracker_type():
    with pytest.raises(ValueError):
        await recorder.record(tracker_type="bogus", ...)
```

`tests/test_usage_llm_call_repository.py`:
```python
def test_record_inserts_row_with_null_cost(tmp_path):
    # bootstrap db, insert row with cost_usd=None, round-trip read

def test_record_raises_on_invalid_call_outcome(tmp_path):
    # call_outcome='bogus' raises ValueError before any DB touch

def test_all_six_call_outcomes_insert_successfully(tmp_path):
    # iterate CALL_OUTCOMES, insert one row each, verify count=6
```

`tests/test_openrouter_client_instrumentation.py`:
```python
async def test_usage_capture_populated_from_response(mock_httpx):
    # mock response with usage.prompt_tokens=10, completion_tokens=20, cost=0.0034
    # call answer_grounded(); unpack (text, capture)
    # assert capture.prompt_tokens==10, capture.completion_tokens==20, capture.cost_usd==0.0034

async def test_missing_cost_gives_none(mock_httpx):
    # response has no 'cost' in usage dict
    # capture.cost_usd is None

async def test_error_row_on_http_failure(mock_httpx, recorder_mock):
    # mock httpx to raise HTTPStatusError
    # assert recorder.record called with call_outcome='error', prompt_tokens=0
    # assert exception re-raised
```

`tests/test_grounded_rag_call_outcome_propagation.py`:
```python
async def test_successful_answer_writes_customer_visible_answer(mock_llm, recorder):
    # mock both answer_grounded and verify_grounding to succeed
    # both rows tagged 'customer_visible_answer'

async def test_verifier_rejected_tags_both_rows(mock_llm, recorder):
    # verifier returns NOT_GROUNDED
    # both rows tagged 'verifier_rejected'

async def test_guardrails_blocked_tags_both_rows(mock_llm, recorder):
    # guardrails return invalid
    # both rows tagged 'guardrails_blocked'

async def test_sentinel_writes_escalated_to_hitl(mock_llm, recorder):
    # answer_grounded returns 'ESCALATE_TO_HUMAN'
    # one row tagged 'escalated_to_hitl'; no verifier call

async def test_llm_error_writes_error_row(mock_llm, recorder):
    # answer_grounded raises; single 'error' row from _chat's error path
    # answerer returns handled=False
```

### Project context notes
- `AnswerContext.project_id: int | None` — the field exists (see `services/api/app/answerers/__init__.py:30`).
  The `project_id` is `None` for legacy/unconfigured flows; recorder writes are silently skipped when `project_id is None`.
- `openrouter_client` is a module-level singleton in `main.py` (line ~219). All answerers and
  analyzers receive it via constructor injection — no service-locator anti-pattern.
- The existing `@app.on_event("startup")` / `@app.on_event("shutdown")` pattern is the correct
  lifecycle hook — see `sync_telegram_identity_on_startup` (line ~3558) and `validate_llm_models_on_startup`
  (line ~3594) in `main.py`.
- `_chat` currently returns `str`. All test files that mock `openrouter_client._chat` or call
  `answer_grounded` / `verify_grounding` must be updated to expect the new return type.
- Do NOT add a `usage_recorder_started` call inside `UsageRecorder.__init__` — the consumer task
  must only start when the FastAPI event loop is running (inside a startup hook).

### References
- Planning story: `_bmad-output/planning-artifacts/epics/stories/epic-14/story-14-02-llm-instrumentation-and-recorder.md`
- Existing `OpenRouterClient`: `services/api/app/openrouter_client.py`
- Existing `GroundedRagAnswerer`: `services/api/app/answerers/grounded_rag.py`
- `ClientMaterialsAnalyzer`: `services/api/app/sales/client_materials_analyzer.py`
- Existing lifecycle hooks: `services/api/app/main.py:3557–3623`
- `AnswerContext.project_id`: `services/api/app/answerers/__init__.py:30`
- 14.01 implementation doc (pattern reference): `_bmad-output/implementation-artifacts/14-01-usage-db-schema-and-migration.md`
- Usage repo skeletons: `services/api/app/usage/repositories.py`

## Dev Agent Record

### Agent Model Used

claude-sonnet-4-6

### Debug Log References

### Completion Notes List

- All 8 tasks complete with 100% line coverage on `recorder.py`, modified sections of `repositories.py`, instrumented diff of `openrouter_client.py`, outcome-propagation diffs in `grounded_rag.py` and `client_materials_analyzer.py`.
- asyncio.Queue event loop binding issue (Python 3.11 `_LoopBoundMixin`): fixed by recreating `self._queue` inside `start()` so it binds to the current test event loop.
- `"цена?"` question in propagation tests replaced with `"когда приедет курьер?"` to avoid `is_service_catalog_query` detection path. Default answer length increased to pass `evaluate_suggestion` guardrail (`insufficient_content` fires for strings < 10 chars).
- `test_guardrails_blocked_tags_both_rows` patches `evaluate_suggestion` directly instead of loading hedge phrases via `prompts.get_prompt` (which returns None in tests, yielding an empty phrase list that never blocks).
- 7 existing e2e/unit test files updated to unpack tuple returns from `answer_grounded` and `verify_grounding`; 5 test files updated to accept `project_id` kwarg in `complete_json` stubs.
- `ruff check .` clean on 2026-06-11.

### File List

New files:
- `services/api/app/usage/recorder.py`
- `tests/test_usage_recorder.py`
- `tests/test_usage_llm_call_repository.py`
- `tests/test_openrouter_client_instrumentation.py`
- `tests/test_grounded_rag_call_outcome_propagation.py`
- `tests/test_client_materials_analyzer_call_outcome.py`
- `tests/test_usage_recorder_lifespan.py`
- `tests/e2e/test_e2e_epic14_llm_round_trip.py`

Modified files:
- `services/api/app/usage/repositories.py`
- `services/api/app/openrouter_client.py`
- `services/api/app/answerers/grounded_rag.py`
- `services/api/app/sales/client_materials_analyzer.py`
- `platform_common/settings.py`
- `services/api/app/main.py`
- `.env.example`
