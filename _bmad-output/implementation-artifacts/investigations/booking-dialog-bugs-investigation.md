# Investigation: Booking-dialog bugs (cancellation, price-funnel, off-hours wording, duplicate/nondeterministic replies)

## Hand-off Brief

1. **What happened.** Four live-tested defects in the sales booking dialog: (1) cancellation utterances fall through to the generic "передам коллегам" handoff because there is **no cancellation intent anywhere in the sales stack** [Confirmed]; (2) a first-turn "8 человек, сколько стоит?" drops the price question and jumps to dates because the price intercept is **structurally unreachable before scoping** [Confirmed]; (3) an off-hours (23:00) request is mislabeled "занято" because every `STATUS_UNAVAILABLE` is collapsed to the busy line, **discarding the already-correct `outside_working_hours` reason** [Confirmed]; (4) replies are nondeterministic because **no OpenRouter call sets `temperature`** (defaults ~1.0), while duplicate-sends are **already fixed in-tree** (Story 12.24) and the live sighting was a stale deploy [Confirmed nondeterminism / Deduced duplicate-sends].
2. **Where the case stands.** All four root causes Confirmed by read code paths with `path:line`. Concluded; ready for fixes.
3. **What's needed next.** Implement four fixes as a tracked epic (one story each), each TDD with 100% coverage. Bug #4's duplicate-send half is primarily a redeploy/verify action, not a code change.

## Case Info

| Field            | Value                                                                      |
| ---------------- | -------------------------------------------------------------------------- |
| Ticket           | N/A (live Telegram test, 2026-05-31, persona "Анна Иванова")               |
| Date opened      | 2026-05-31                                                                 |
| Status           | Concluded                                                                  |
| System           | Semantaix api service, Python 3.11, Docker stack; branch eager-banach-60211a |
| Evidence sources | Source code (services/api, services/bot_gateway), git log, existing tests  |

## Problem Statement

User live-tested 8 booking conversations and fixed bugs A+B (absolute-date busy-check, applied as `booking-dialog-busy-check-fix.patch`). Four bugs remained, flagged as separate root causes needing approval before touching the LLM/intent layer:
- Cancellation question → canned "передам коллегам" reply (intent routing).
- "8 человек, сколько стоит?" → price/advice ignored, jumps to asking dates.
- Off-hours (23:00) labeled "занято" instead of "вне рабочих часов".
- Duplicate identical sends + nondeterministic replies to the same message.

## Confirmed Findings

### Finding 1 (Bug #1): No cancellation intent exists in the sales stack

**Evidence:** `services/api/app/sales/russian_sales_intent.py` + `data/russian_sales_intent.txt` (booking-positive seeds only, no cancel token); `services/api/app/sales/turn_intent.py:48-180` (`classify_turn` has only `catalog_ask`/`concept_ask`/`price_ask`, no `cancel`); `services/api/app/sales/decline.py:23-53` (declines a scoping field, not a booking). Canned line emitted at `services/api/app/sales/sales_persona_answerer.py:1478` (`SLOT_FREE_HANDOFF_LINE`/`SCOPING_COMPLETE_HANDOFF_LINE`, defined `:116-121`) and `:1081`.

**Detail:** The lemma `отменить` is matched by nothing in `sales/`, `answerers/`, or `data/`. Worse, booking seeds (`бронь`, `запись`) make `is_sales_intent("хочу отменить запись")` return **True**, pulling the cancellation INTO the booking funnel → generic handoff. Two forks (both verified by running the classifiers live): with a booking noun → sales funnel → handoff; bare "можно отменить?" → `is_sales_intent`=False → falls through to `GroundedRagAnswerer` → generic HITL. `followup_cancel_hook.py` is a false friend (cancels the proactive +24h nudge, not a booking).

### Finding 2 (Bug #2): Price intercept is structurally unreachable on the first/greeting turn

**Evidence:** `classify_turn` correctly tags "8 человек, сколько стоит?" as `price_ask` (`turn_intent.py:144-180`), but it is consulted ONLY in `_maybe_handle_aside` (`sales_persona_answerer.py:1528`), which runs ONLY when `current_stage ∈ {scoping, pitching}` (`sales_persona_answerer.py:596`, `_ASIDE_INTERCEPT_STAGES` at `:183-185`). First contact has `state is None` → `_dispatch` routes to `_handle_greeting` (`:582-585`), which never classifies the turn and never calls `price_lookup`.

**Detail:** Greeting LLM extracts `headcount=8`, the greeting prompt forbids quoting prices (`system_prompts/sales_greeting.txt:12`), stage transitions to `scoping` (`:725`), and next-field logic asks the top-missing field `dates` (`sales_greeting.txt:9`; required keys `intent.py:22-28`). Direct simulation reproduced: reply "На какую дату планируете?", `stage_after=scoping`, zero `price_lookup` calls. Same string from an existing `scoping` state routes to pricing correctly → the bug is **greeting-only**. Secondary: `price_lookup._build_query` ignores `intent`/`headcount` entirely (`price_lookup.py:99-110,131-160`) — group size never drives the quoted price (out of scope for the minimal fix; documented).

### Finding 3 (Bug #3): Off-hours mislabeled "занято" — reason dropped at the presentation layer

**Evidence:** Engine `compute_availability` checks reasons in order `in_past → outside_lookahead → date_exception → wrong_service_day → outside_working_hours → busy` (`calendar/availability.py:14-18`), off-hours branch `:256-261` (`REASON_OUTSIDE_WORKING_HOURS`, `:39`), busy branch `:263-266`. Reason preserved up via `RequestedAvailability(reason=result.reason)` (`calendar/requested_time_check.py:172-174`). But both consumers branch only on `status`: `_complete_booking` (`sales_persona_answerer.py:1128`) and `_maybe_intercept_busy_slot` (`:1443`) collapse every `STATUS_UNAVAILABLE` into `_propose_alternative_or_handoff`, which unconditionally prepends `SLOT_BUSY_LINE` ("занято", `:122`) at `:1322`.

**Detail:** Per-service working-hours window already exists end-to-end (`settings_repository.py:83`, `project_services_repository.py:132`, enforced `availability.py:145-152,256-261`). Only the customer-facing copy "вне рабочих часов" is missing (zero hits repo-wide). Same collapse also mislabels `wrong_service_day` and `date_exception` as "занято" — same root cause, broader blast radius. Pure presentation mislabel (case a), NOT a missing concept.

### Finding 4 (Bug #4a): Nondeterministic replies — no temperature on any OpenRouter call

**Evidence:** `OpenRouterClient._chat` payload `services/api/app/openrouter_client.py:110` = `{"model", "messages"}` only; `complete_json` `:205-209` adds only `response_format`. No `temperature`/`top_p`/`seed`; models default to ~1.0. No settings knob (`platform_common/settings.py` has only model names). User-visible drift: scoping/greeting `next_question` (`sales_persona_answerer.py:683,778`), grounded answer (`answerers/grounded_rag.py:185`), grounding verifier (`grounded_rag.py:222`, can flip deliver-vs-escalate).

**Detail:** Direct, sufficient cause of "different replies to the same message." Deterministic fallback questions already exist (`schema.question_for(...)`, used `:845,863`); only LLM-authored questions drift.

## Deduced Conclusions

### Deduction 1 (Bug #4b): Duplicate sends are already fixed in-tree; live sighting was a stale deploy

**Based on:** Finding that two-layer idempotency exists — gateway `INSERT OR IGNORE` on `UNIQUE(source_message_id)` (`services/bot_gateway/app/persistence.py:39,85-92`) and API atomic `claim_inbound(trace_id)` + finalized-replay gate (`services/api/app/main.py:2314,2340`, `answer_trace.py:184-203`), keyed on a trace_id identical across Telegram retries (`_derive_trace_id` = `tg-update-{update_id}`, bot_gateway `main.py:2305`). Webhook returns 200 fast via BackgroundTask (`main.py:2704-2730`).

**Reasoning:** `git` shows Story 12.24 (`727676d`, "idempotent inbound delivery — no duplicate sends on slow turns") + hardening (`c35c903`) committed **2026-05-31**, the same day as the live test. Tests cover both sequential and in-flight duplicate windows (`tests/test_api_conversations_inbound.py:260,304`; `tests/test_answer_trace_claim.py`; `tests/test_bot_gateway_webhook.py:104,146`). Project memory documents stale containers / nginx 502s silencing the bot.

**Conclusion:** The duplicate-send defect is closed in code; the live duplicates were almost certainly produced by a container predating `727676d`. Action is verify-deploy/rebuild, not a code change. (To confirm: check deployed image commit and look for `telegram_duplicate_update_ignored` / `inbound_idempotent_replay` log lines around the incident.)

## Source Code Trace

| Element       | Detail                                                                                          |
| ------------- | ----------------------------------------------------------------------------------------------- |
| Bug #1 origin | `sales/sales_persona_answerer.py:582-585` (greeting captures cancel) + `:1478`/`:1081` (handoff) |
| Bug #2 origin | `sales/sales_persona_answerer.py:596` (`_ASIDE_INTERCEPT_STAGES` gate) + `:582-585` (greeting)   |
| Bug #3 origin | `sales/sales_persona_answerer.py:1128`,`:1443` (status-only branch) + `:1322` (SLOT_BUSY_LINE)   |
| Bug #4a origin| `openrouter_client.py:110`,`:205-209` (no temperature)                                           |
| Bug #4b       | Fixed in-tree (`main.py` claim/dedup); deploy-state issue                                        |

## Conclusion

**Confidence:** High for #1, #2, #3, #4a (Confirmed root causes via read code paths). Medium-High for #4b (Deduced fixed-in-tree; would be fully confirmed by deployed-commit/log check).

Four independent root causes, all in or adjacent to `services/api/app/sales/sales_persona_answerer.py`. #1 needs a new cancellation intent + safe routing; #2 needs the price intercept reachable on the first turn; #3 needs the availability reason surfaced into copy; #4a needs a temperature knob (=0 for funnel/verifier); #4b needs a redeploy verification plus optional hardening.

## Recommended Next Steps

### Fix direction (epic = 4 stories)
- **Story 1 — Cancellation intent.** New lemma-based `cancel_intent` detector + gate early in `_dispatch` (before `is_sales_intent`/greeting, independent of stage/state); route to a dedicated HITL escalation with a new reason code + distinct customer copy; cancel the pending +24h follow-up nudge. Do NOT auto-cancel (no booking-of-record).
- **Story 2 — First-turn price intercept.** Make the `price_ask` intercept reachable on the greeting/no-state turn (mirror the early-busy intercept); answer the price (or escalate on miss) and persist extracted `headcount` so it isn't re-asked. Document that group-size-aware pricing is a separate, larger change.
- **Story 3 — Off-hours wording.** Thread `RequestedAvailability.reason` into `_propose_alternative_or_handoff`; add `SLOT_OFF_HOURS_LINE` for `outside_working_hours`; fix the whole mislabel class (`wrong_service_day`, `date_exception`) while here.
- **Story 4 — Determinism + dedup verification.** Add `openrouter_temperature` setting threaded into `_chat`/`complete_json`; pin funnel + verifier calls to 0. Verify the deployed build includes Story 12.24; optionally harden gateway dedup ordering. Tests for temperature plumbing.

### Diagnostic
- Bug #4b: confirm deployed commit ≥ `727676d`; grep bot_gateway/api logs for the dedup markers during the incident window.

## Side Findings
- `price_lookup` ignores `intent`/`headcount` — quoted price is never group-size-aware (`price_lookup.py:99-110`). [Confirmed] Larger change; out of scope for Story 2's minimal fix.
- A cancellation turn currently also re-enqueues the +24h follow-up (`sales_persona_answerer.py:570-571`) — without suppression the customer gets a "still thinking about booking?" nudge a day after asking to cancel. Fold into Story 1. [Deduced]
- Redelivered **operator** commands aren't deduped (gateway dedup sits after operator handlers, bot_gateway `main.py:2621` vs `2390-2609`). Customer booking path is protected. [Confirmed] Optional hardening in Story 4.
