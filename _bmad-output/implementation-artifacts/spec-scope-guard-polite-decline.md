---
title: 'Scope guard — polite decline for off-topic messages'
type: 'feature'
created: '2026-05-29'
status: 'ready-for-dev'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** When a customer asks something outside the bot's scope (e.g. "Какое сегодня число?", "Расскажи анекдот"), no answerer handles it and a HITL ticket is created — creating operator noise for irrelevant questions.

**Approach:** Add a `ScopeGuardAnswerer` as the last answerer in the pipeline. It always handles any message that fell through every previous answerer, picking one random short phrase from a configurable list and sending it — no HITL ticket, no operator DM. The default phrases are short, general-purpose Russian (no mention of "услуги", "запись", or any specific business type).

## Boundaries & Constraints

**Always:**
- `ScopeGuardAnswerer` must be the absolute last entry in `answer_pipeline`.
- Returns `handled=True` with `response_mode="scope_decline"` — this hits the normal delivery path in `conversations_inbound` (no ticket, no operator DM).
- Phrase list is configurable: `hitl_runtime_config` key `scope_decline_messages` (newline-separated) overrides the settings default. Same seam as `inbound_ack_message`.
- Default phrases must not mention "услуги", "запись", "запись", calendar, or any domain-specific term — must work for any business type (products, services, etc.).
- 100% coverage maintained.

**Ask First:**
- If the operator ever needs to receive HITL notifications for a specific subset of unhandled messages (e.g. complaints), stop and discuss before scoping that here.

**Never:**
- Modify `GroundedRagAnswerer` or any other existing answerer.
- Add LLM calls — the scope guard is a pure pass-through with a fixed text response.
- Create a HITL ticket or operator DM for scope-declined messages.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output |
|---|---|---|
| Off-topic question | "Какое сегодня число?" — no sales state, no RAG hit | One random short phrase sent to customer, no ticket |
| General chit-chat | "Привет, расскажи анекдот" | One random short phrase (may differ from above) |
| Service question (in KB) | "Сколько стоит прокат?" — RAG hit | Handled by GroundedRagAnswerer; scope guard never fires |
| Booking request | "Хочу забронировать багги" | Handled by SalesPersonaAnswerer; scope guard never fires |
| Custom decline text | operator set `scope_decline_message` via runtime config | Custom text delivered |

</frozen-after-approval>

## Code Map

- `services/api/app/answerers/scope_guard.py` — new file: `ScopeGuardAnswerer` class
- `platform_common/settings.py` — new `scope_decline_message` field
- `services/api/app/main.py` — import + `_effective_scope_decline_message()` helper + append to pipeline
- `tests/test_scope_guard_answerer.py` — new test file

## Tasks & Acceptance

**Execution:**
- [ ] `platform_common/settings.py` -- add `scope_decline_messages: str` (newline-separated list of short phrases, no mention of "услуги"/"запись"/calendar); default is 5 short direct Russian alternatives: "Этим не занимаюсь.\nНе по адресу, увы 🙂\nНе смогу тут помочь.\nЭто не ко мне.\nЗдесь не помогу."
- [ ] `services/api/app/answerers/scope_guard.py` -- create `ScopeGuardAnswerer` with `name = "scope_guard"`; accepts `phrases_getter: Callable[[], str]` (returns the newline-separated string); `try_answer()` splits by newline, strips blanks, picks one with `random.choice`, returns `AnswerResult(handled=True, text=<chosen>, response_mode="scope_decline")`
- [ ] `services/api/app/main.py` -- add `_effective_scope_decline_messages()` (key `scope_decline_messages`, fallback to `settings.scope_decline_messages`); import `ScopeGuardAnswerer`; append `ScopeGuardAnswerer(phrases_getter=_effective_scope_decline_messages)` as last element of `answer_pipeline`
- [ ] `tests/test_scope_guard_answerer.py` -- cover: try_answer returns handled=True with scope_decline mode; chosen text is one of the phrases from getter; multiple calls produce values from the phrase list (not always identical); pipeline wiring (scope_guard is last); runtime config override respected; integration via TestClient: off-topic message gets one of the default phrases, no HITL ticket

**Acceptance Criteria:**
- Given any message not handled by SalesPersonaAnswerer, CalendarAvailabilityAnswerer, or GroundedRagAnswerer, when processed by `/conversations/inbound`, then the customer receives the scope-decline text and no HITL ticket is created.
- Given the operator has set `scope_decline_messages` in runtime config (newline-separated), when scope guard fires, then one of those custom phrases is delivered (not the settings default).
- Given a valid service question with a RAG hit, when processed, then scope guard does not fire and the normal answer is delivered.
- Given `scope_guard` is in the pipeline, when `answer_pipeline.answerers` is inspected, then `scope_guard` is the last element.

## Verification

**Commands:**
- `pytest tests/test_scope_guard_answerer.py -v` -- expected: all pass
- `pytest --cov --cov-config=.coveragerc --cov-report=term-missing` -- expected: 100% coverage
- `ruff check .` -- expected: no errors
