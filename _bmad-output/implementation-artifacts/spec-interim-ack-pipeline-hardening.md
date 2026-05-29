---
title: 'Interim ack for slow-path messages + pipeline hardening'
type: 'feature'
created: '2026-05-29'
status: 'ready-for-dev'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Messages that enter the LLM-backed sales pipeline (booking requests like "хочу забронировать багги завтра в 14:00") produce no customer-visible feedback for several seconds; in some cases (unhandled pipeline exception) they produce no response at all.

**Approach:** (1) Before running `answer_pipeline.run()`, detect if the message is a sales-intent / ongoing-sales-state message and send a configurable interim "Проверяю, минуточку…" message immediately. (2) Wrap the pipeline execution in a try/except so any unhandled exception falls back to sending the standard HITL ack and creating a ticket — the customer is never left in total silence.

## Boundaries & Constraints

**Always:**
- Interim is sent only for messages on slow paths: active sales state OR sales-intent match. Non-sales messages (RAG-only, escalation coalesce) do not get an interim.
- Interim text is configurable via the same runtime_config/settings seam as `inbound_ack_message` (key: `inbound_interim_message`).
- Pipeline exception handler must create a HITL ticket and persist the trace — same outcome as a clean escalation, just triggered by an unexpected error.
- 100% test coverage maintained.

**Ask First:**
- If the interim text needs to come from a per-project prompt (project_prompt_repository), stop and confirm before adding that layer.

**Never:**
- Send interim for every customer message — must be gated on sales context.
- Suppress or swallow the exception after logging it — always escalate.
- Change the happy-path (non-exception) pipeline flow.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior |
|---|---|---|
| Sales-intent message, no prior state | "хочу забронировать багги завтра в 14:00" | Interim sent immediately, then normal pipeline answer |
| Active sales state, continuation | "ok, взрослый" (scoping follow-up) | Interim sent (state exists), then pipeline continues scoping |
| Non-sales RAG question | "какой у вас график работы?" | No interim, pipeline runs, answer sent |
| Pipeline raises unexpectedly | Any message, LLM transport failure | Interim may or may not have fired; HITL ack sent to customer, ticket created, error logged |
| chat_id is None | Internal call without chat context | Interim skipped (no target), pipeline still runs |

</frozen-after-approval>

## Code Map

- `platform_common/settings.py` — add `inbound_interim_message: str` field
- `services/api/app/main.py` — add `_effective_inbound_interim_message()`, `_should_send_interim()`, interim send before `answer_pipeline.run()`, try/except around pipeline
- `services/api/app/sales/russian_sales_intent.py` — `is_sales_intent()` used for intent gate
- `services/api/app/sales/state_repository.py` — `SalesStateRepository.get(chat_id)` for active-state gate
- `tests/test_inbound_interim_ack.py` — new test file covering all I/O matrix scenarios

## Tasks & Acceptance

**Execution:**
- [ ] `platform_common/settings.py` -- add `inbound_interim_message: str = "Проверяю, минуточку… 🙂"` field after `inbound_ack_message`
- [ ] `services/api/app/main.py` -- add `_effective_inbound_interim_message()` helper (same pattern as `_effective_inbound_ack_message` but key `inbound_interim_message`, no project-prompt override layer needed); add `_should_send_interim(text, chat_id)` pure function that returns True when active sales state OR sales intent detected; in `conversations_inbound`, after `cancel any pending +1d nudge` block and before `answer_pipeline.run()`, call `_should_send_interim` and if true send interim via `_safe_send_message`; wrap `answer_pipeline.run()` call in try/except, on exception send ack + create HITL ticket + log
- [ ] `tests/test_inbound_interim_ack.py` -- cover: interim fired for sales-intent message, interim fired for active-state continuation, interim NOT fired for non-sales message, pipeline exception produces HITL ack (not silence), chat_id=None skips interim gracefully

**Acceptance Criteria:**
- Given a message with booking/sales intent and no prior state, when the `/conversations/inbound` endpoint processes it, then the customer receives an interim "Проверяю" message before the final answer.
- Given an ongoing sales conversation (active state in DB), when the next customer message arrives, then the interim is sent regardless of whether the message text matches intent.
- Given a non-sales RAG question with no active sales state, when processed, then no interim message is sent.
- Given the `answer_pipeline.run()` raises an exception, when processing any customer message, then the customer receives the standard ack message, a HITL ticket is created, the error is logged, and the endpoint returns without re-raising.
- Given `chat_id` is None, when interim logic runs, then no Telegram send is attempted and no exception is raised.

## Verification

**Commands:**
- `pytest tests/test_inbound_interim_ack.py -v` -- expected: all pass
- `pytest --cov --cov-config=.coveragerc --cov-report=term-missing` -- expected: 100% on platform_common/ and services/
- `ruff check .` -- expected: no errors
