# Story 12.30: Deterministic LLM sampling (no more different replies to the same message)

Status: review

## Story

As a **customer (and as an operator watching the bot)**,
I want **the same message to produce the same booking question / answer / escalation verdict**,
so that **the bot doesn't contradict itself and a borderline answer isn't delivered one time and escalated the next**.

**Problem (observed live, багги, 31 May 2026, "Анна Иванова"):** the bot gave nondeterministic (different) replies to the same customer message.

**Root cause** (`services/api/app/openrouter_client.py`): **no OpenRouter call set a sampling temperature** — `_chat` (`:110`) and `complete_json` (`:205-209`) sent only `model` + `messages`, so the model defaulted to temperature ≈ 1.0. Every user-visible LLM output drifted run-to-run: the scoping/greeting `next_question` (the booking funnel's visible questions), the grounded answer, and — most insidiously — the grounding verifier, which could flip *deliver* vs *escalate* for the same answer. There was no settings knob to control it.

(The "duplicate identical sends" half of the live report is a **separate, already-fixed** issue — Story 12.24's two-layer inbound idempotency, committed the same day as the test. The live duplicates were almost certainly a stale container. See "Duplicate sends" below for the verify step and the remaining operator-command follow-up.)

## Acceptance Criteria

1. **Configurable default temperature.** A new `openrouter_temperature` setting (default `0.0`) drives generative calls; a fresh `OpenRouterClient` reads it into `client.temperature`.
2. **Generative calls send it.** `answer_grounded` (and `summarize_offerings`) include `temperature` in the payload, taking the configured default; raising the setting raises only these calls.
3. **Funnel pinned to 0.** `complete_json` (greeting/scoping extraction + next-question, services/materials extraction) always sends `temperature=0.0`, regardless of the configured default — the booking funnel is reproducible.
4. **Verifier pinned to 0.** `verify_grounding` always sends `temperature=0.0` — the deliver-vs-escalate verdict is reproducible.
5. **No regression.** Existing OpenRouter / grounded-RAG / pipeline behavior is unchanged except for the added payload field.
6. **Gates green.** `ruff check .` clean; full suite at 100% coverage on `platform_common/` + `services/`.

## Tasks / Subtasks

- [x] **Setting** (AC 1) — `openrouter_temperature: float = 0.0` in `platform_common/settings.py`.
- [x] **Client plumbing** (AC 1–4) — `OpenRouterClient.__init__` reads `self.temperature`; `_chat` gains a `temperature` override (defaults to `self.temperature`); `complete_json` and `verify_grounding` hard-pin `_DETERMINISTIC_TEMPERATURE = 0.0`; `answer_grounded`/`summarize_offerings` use the configurable default.
- [x] **TDD tests** (AC 1–4) — added to `tests/test_openrouter_client.py`: default-from-settings, generative uses the configured value, `complete_json` pinned to 0 even when the default is raised, `verify_grounding` pinned to 0 even when the default is raised.
- [x] **Gates** (AC 6) — `ruff` clean; 25 OpenRouter tests + 185 LLM-touching tests green.

## Dev Notes

- **Files:** `platform_common/settings.py` (`openrouter_temperature`); `services/api/app/openrouter_client.py` (`_DETERMINISTIC_TEMPERATURE`, `__init__`, `_chat` temperature param, `complete_json` + `verify_grounding` pinned). No protocol/fake churn: temperature lives entirely inside the real client, so the SalesPersonaAnswerer's `_OpenRouter` protocol and every test fake are untouched.
- **Why pin the funnel + verifier but not the answer:** structured extraction and the grounding verdict must be reproducible (a coin-flip verdict is the worst failure mode — it delivers a borderline answer half the time). The grounded *answer* can tolerate, and may benefit from, mild phrasing variety, so it stays on the tunable default (still 0.0 until an operator raises it).
- **Tradeoff (documented):** even at `temperature=0` LLM outputs are not byte-stable across model/provider updates or load-balanced backends, and the free-form `next_question` can still vary in wording. For fully stable booking questions the deterministic `schema.question_for(...)` fallbacks already exist; pinning temperature removes the dominant, avoidable source of drift.

### Duplicate sends — verify step + follow-up (Bug #4, second half)

- **Customer duplicate sends are already fixed in-tree (Story 12.24)** — two-layer idempotency on the Telegram update id (gateway `UNIQUE(source_message_id)` + api atomic `claim_inbound(trace_id)`), webhook returns 200 fast via BackgroundTask. **Action: verify the deployed build includes `727676d` and rebuild the stack;** confirm by grepping bot_gateway/api logs for `telegram_duplicate_update_ignored` / `inbound_idempotent_replay` around an incident. No code change needed for the customer path.
- **Operator-command dedup gap (deferred to Story 12.31).** The gateway dedup (`persist_normalized_message`) sits *after* the operator/command handlers in `services/bot_gateway/app/main.py` (dedup at `:2621`, operator handlers `:2404-2562`), so a redelivered operator command (notably the slow NL-service handler that makes an LLM call) can double-act. **This is NOT a cheap reorder:** `persist_normalized_message` writes the message into the Telegram transcript (`role="user"`, `semantaix_story1.db`) which feeds `/knowledge/extract`, so moving it above the command handlers would pollute the transcript + knowledge extraction with command text; and a separate webhook-entry update-id claim entangles with Story 12.24's existing idempotency layers and their tests. The safe design is a dedicated, non-transcript idempotency store claimed at webhook entry — tracked as **Story 12.31** rather than rushed here. Customer booking messages are already protected (they fall through to the deduped branch).

### References

- Story 12.24 (idempotent inbound delivery) — the customer-side duplicate-send fix this leans on.
- Investigation: `_bmad-output/implementation-artifacts/investigations/booking-dialog-bugs-investigation.md` (Finding 4 + Deduction 1).
