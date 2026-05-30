# Story 12.13: Deferred interim ack — "минуточку" only when the answer is actually slow

Status: ready-for-dev

## Story

As a **customer**,
I want the **"Проверяю, минуточку… 🙂"** message to appear **only when the bot is genuinely doing lengthy work**,
so that **quick replies (like scoping questions) don't get a pointless "please wait" before every turn**.

**Problem (observed live):** the interim ack is sent before *every* message in an active sales conversation — `_should_send_interim` returns True whenever a sales state exists or the text is sales-intent. So every fast scoping turn (a ~1–2s LLM call) is preceded by "минуточку", which is noisy and misleading. The ack was meant for the slow grounded-RAG / calendar / pricing paths.

## Acceptance Criteria

1. **Fast turns get no ack.** Given an eligible (sales-path) message, when the answer pipeline returns within the interim delay (`inbound_interim_delay_seconds`, default ~8s), then **no** interim ack is sent — only the real answer.
2. **Slow turns still get the ack.** Given an eligible message whose pipeline run exceeds the delay, when the threshold elapses, then the interim ack is sent once, and the real answer follows when ready.
3. **Eligibility unchanged.** Non-sales / non-eligible messages still never get an interim (the existing `_should_send_interim` gate is preserved as the *eligibility* filter; the timer is added on top).
4. **Ordering + single send.** The interim (if sent) always precedes the real answer; the interim is sent at most once per inbound; a pipeline failure path still behaves as today.
5. **Configurable + gates green.** `inbound_interim_delay_seconds` is a `Settings` value (overridable via runtime config like other inbound knobs). `ruff` clean; full suite 100% coverage; tests for fast→no-ack, slow→ack-then-answer, ineligible→no-ack, and the delay boundary (injected clock/sleep).

## Tasks / Subtasks

- [ ] **Add the delay setting** (AC: 5) — `Settings.inbound_interim_delay_seconds` (default ~8.0) + an `_effective_inbound_interim_delay()` resolver mirroring the other `_effective_inbound_*` helpers.
- [ ] **Race the pipeline against a timer** (AC: 1, 2, 4) — in `/conversations/inbound`, when `_should_send_interim(...)` is True: start the pipeline as a task, then `await asyncio.wait({pipeline_task}, timeout=delay)`. If it's still pending → send the interim ack, then `await` the task. If it finished first → no interim. Then proceed with the existing answer-send/escalation logic on the pipeline result.
- [ ] **Keep failure handling intact** (AC: 4) — the existing `try/except` around `answer_pipeline.run` must still catch pipeline exceptions (now surfaced via the awaited task); the interim send stays best-effort (`_safe_send_message`).
- [ ] **Tests** (AC: all) — inject a controllable pipeline (fast vs slow) and a fake clock/sleep so the timer boundary is deterministic: fast → interim NOT sent; slow → interim sent once then answer; ineligible → never; pipeline-raises → handled as today. 100% coverage.

## Dev Notes

- **Files:** `services/api/app/main.py` (the `/conversations/inbound` handler around the `_should_send_interim` + `answer_pipeline.run` block, ~lines 2292–2305), `platform_common/settings.py` (+ `inbound_interim_delay_seconds`), plus the `_effective_inbound_interim_delay` helper near the other `_effective_inbound_*` ones.
- **Mechanism:** `task = asyncio.create_task(answer_pipeline.run(...))`; `done, pending = await asyncio.wait({task}, timeout=delay)`; `if task in pending: <send interim>`; `result = await task`. This self-adjusts — no need to classify which turns are slow.
- **Time injection:** the project bans ambient `datetime.now()` in branch logic and wants test-reachable timing — drive the timeout via an injected delay value and let tests use a fast/slow fake pipeline (and `asyncio` virtual time or a tiny real delay) so the boundary is deterministic without flakiness.
- **Don't regress** the eligibility gate (`_should_send_interim`) or the separate post-answer `ack_message` / escalation sends — only the *interim* timing changes.
- **Conventions:** async I/O; best-effort interim via `_safe_send_message`; ruff E/F/I line-100; 100% coverage.

### References

- [Source: services/api/app/main.py#_should_send_interim (~1597) and the inbound interim send (~2292)]
- [Source: platform_common/settings.py#inbound_interim_message]
- Live evidence: every scoping turn preceded by "Проверяю, минуточку… 🙂".

## Dev Agent Record

### Agent Model Used

claude-opus-4-7 (Claude Code)

### Completion Notes List

- `/conversations/inbound` now runs the pipeline as a task and `asyncio.wait({task}, timeout=delay)`; the interim ack is sent only when the task is still pending after the delay. Eligibility gate (`_should_send_interim`) unchanged; failure handling preserved (`await pipeline_task` re-raises into the existing `except`).
- `Settings.inbound_interim_delay_seconds` (default 8.0) + `_effective_inbound_interim_delay()` (runtime config / settings, ValueError-safe).
- Existing "interim sent before pipeline" test replaced by two: slow pipeline → ack then answer; fast pipeline → answer only. ruff clean; full suite 100%.

### File List

- `services/api/app/main.py` (timer race in the inbound handler + `_effective_inbound_interim_delay`)
- `platform_common/settings.py`, `.env.example` (`inbound_interim_delay_seconds`)
- `tests/test_inbound_interim_ack.py` (modified — slow/fast + delay-resolver tests)
