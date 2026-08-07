---
title: 'Human-like pause before Telegram customer replies'
type: 'feature'
created: '2026-08-06'
status: 'draft'
context:
  - '{project-root}/_bmad-output/project-context.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Customer-facing Telegram replies are delivered immediately, which
makes the bot feel mechanical. The user wants a short, human-like pause and a
visible typing indicator before the bot sends its answer.

**Approach:** Add an asynchronous customer-reply delay at the Telegram sender
boundary. During the delay, send Telegram's `typing` chat action. Apply this
only to customer-facing bot replies; operator notifications, commands,
calendar connection messages, and direct HITL replies stay immediate. The
first neutral greeting should say `Здравствуйте! Чем могу помочь?` — greeting
the customer without assuming a domain or service.

## Boundaries & Constraints

**Always:** Keep the delay non-blocking for other chats; use the existing
injected async transport; keep the default short enough not to threaten the
inbound pipeline/forward timeout; make the delay configurable through the
existing Settings/runtime-config pattern; preserve message ordering and
delivery/error semantics.

**Ask First:** A materially different default delay, per-project/persona
delays, or typing indicators for the linked Telegram user-account channel.

**Never:** Do not use `time.sleep`; do not delay operator/HITL messages; do not
make the pipeline wait before it has produced an answer; do not send a second
customer message merely to simulate typing; do not hide Telegram API failures.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| HAPPY_PATH | Bot customer answer, positive configured delay | `typing` action, async delay, then one answer message | Answer still sends if typing action cannot be delivered |
| ZERO_DELAY | Runtime/config value is `0` | Send one answer immediately; no typing action or sleep | N/A |
| INVALID_DELAY | Runtime value is malformed or negative | Use safe configured default, never raise | Log/handle as existing config fallback does |
| OPERATOR_MESSAGE | HITL/operator notification | Immediate send without typing/delay | Preserve current best-effort handling |

</frozen-after-approval>

## Code Map

- `platform_common/settings.py` -- default customer reply delay configuration.
- `.env.example` -- documents the optional local/Docker configuration value.
- `services/api/app/main.py` -- resolves runtime delay and marks only customer-facing sends for humanization.
- `services/api/app/telegram_bot_sender.py` -- sends Telegram `typing` action and awaits the non-blocking delay before `sendMessage`.
- `tests/test_telegram_bot_sender.py` -- covers typing action, delay, and zero-delay sender behavior.
- `tests/test_api_conversations_inbound.py` -- verifies customer sends receive delay options while operator sends do not.

## Tasks & Acceptance

**Execution:**

- [ ] `platform_common/settings.py` and `.env.example` -- add the named default delay setting -- keep the value configurable without embedding a magic delay in code.
- [ ] `services/api/app/telegram_bot_sender.py` -- add `sendChatAction` support and an async delay option on customer message delivery -- make Telegram presentation behavior reusable and non-blocking.
- [ ] `services/api/app/main.py` -- add runtime-config resolution and pass humanization only for customer-facing bot sends -- protect operator/system messages from added latency.
- [ ] `tests/test_telegram_bot_sender.py` and `tests/test_api_conversations_inbound.py` -- cover all matrix cases and customer/operator routing -- prevent regressions in delivery semantics.

**Acceptance Criteria:**

- Given a customer-facing bot answer and the default positive delay, when the
  answer is delivered, then Telegram receives `typing` before the one answer
  message and the event loop remains available to other requests.
- Given a first bare greeting, when the bot answers, then the text begins with
  `Здравствуйте!` and asks `Чем могу помочь?` without domain-specific wording.
- Given a zero or invalid delay, when a customer answer is delivered, then it
  sends exactly once without an exception; zero skips the typing action and
  invalid input falls back safely.
- Given an operator notification or HITL reply, when it is delivered, then it
  remains immediate and does not receive customer typing behavior.
- Given the current inbound timeout budget, when the delay is enabled, then
  the existing timeout and escalation behavior remains unchanged.

## Spec Change Log

## Design Notes

The delay belongs at the outbound Telegram boundary rather than inside the
answer pipeline. This preserves answer generation latency, keeps chat handling
concurrent, and lets every customer-facing answer (RAG, sales, calendar, or
fallback) share the same presentation behavior without slowing operator DMs.

## Verification

**Commands:**

- `ruff check .` -- expected: no lint errors.
- `PYTHONPATH=. .venv/bin/pytest -q tests/test_telegram_bot_sender.py tests/test_api_conversations_inbound.py tests/test_inbound_interim_ack.py` -- expected: all focused tests pass.
- `PYTHONPATH=. .venv/bin/pytest --cov --cov-config=.coveragerc --cov-report=term-missing -q` -- expected: full suite passes and coverage reaches 100%.
