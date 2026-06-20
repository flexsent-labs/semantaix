# Investigation: user_gateway double-processes re-delivered MTProto customer messages

## Hand-off Brief

1. **What happened.** **Confirmed (code):** the api dedups `/conversations/inbound` solely on
   `trace_id`, but `user_gateway`'s `message_router._forward` mints a fresh random `uuid4` trace_id
   per delivery, so any re-delivered Telethon message bypasses both idempotency gates and is fully
   re-processed (second customer reply, second LLM spend, possible second HITL ticket / sales-state
   transition).
2. **Where the case stands.** Root-cause mechanism is **Confirmed** end-to-end in source. The only
   open link is the real-world **frequency** of Telethon re-delivery (Hypothesized) — which governs
   impact magnitude, not whether the bug exists.
3. **What's needed next.** Patch `message_router` to derive a deterministic trace_id from the
   Telethon message identity (mirror `bot_gateway._derive_trace_id`); track as Epic-15 story 15-05.

## Case Info

| Field | Value |
| --- | --- |
| Ticket | N/A (surfaced by whole-project audit, finding A1/A2) |
| Date opened | 2026-06-19 |
| Status | Concluded (root cause Confirmed; one Hypothesized magnitude link) |
| System | Semantaix @ `origin/main` 475b3d6; `user_gateway` (Telethon MTProto), api inbound seam |
| Evidence sources | Source code (Confirmed); no runtime logs available |

## Problem Statement

Audit finding A1: does the per-operator MTProto customer channel double-process re-delivered
messages because `message_router` generates a random `trace_id` per delivery, defeating the api's
`trace_id`-keyed inbound idempotency? Grade evidence; map blast radius.

## Evidence Inventory

| Source | Status | Notes |
| --- | --- | --- |
| api inbound idempotency | **Available** | `services/api/app/main.py:2585-2634` — both gates keyed on `trace_id` |
| user_gateway forward | **Available** | `services/user_gateway/app/message_router.py:59-80` — random trace_id at :64 |
| Bot-API reference impl | **Available** | `services/bot_gateway/app/main.py:2491` `_derive_trace_id` (deterministic) |
| Runtime logs of a real re-delivery | **Missing** | Would quantify how often Telethon re-delivers; not required to confirm the bug |

## Confirmed Findings

### Finding 1: api inbound idempotency is keyed exclusively on `trace_id`

**Evidence:** `services/api/app/main.py:2595-2597` (`answer_trace_repository.find_by_trace_id`) and
`:2623` (`answer_trace_repository.claim_inbound`).

**Detail:** Two gates — (1) finalized-trace replay gate (returns cached outcome, `deduplicated:
True`, **no** side effects) for retries arriving *after* the first completes; (2) atomic
`claim_inbound` for retries arriving *while the first is in flight*. **Both key on `trace_id`**
(`main.py:2572`: `trace_id = request.trace_id or str(uuid.uuid4())`). If two deliveries carry
different trace_ids, neither gate fires.

### Finding 2: the Bot-API path derives a deterministic trace_id; this is the intended contract

**Evidence:** `services/bot_gateway/app/main.py:2491-2502`.

**Detail:** `_derive_trace_id` returns `f"tg-update-{update_id}"`. The docstring is explicit:
_"Keying on the update_id ensures Telegram retries (which reuse the same update_id) collide on the
same trace_id, so api-side idempotency on /conversations/inbound can short-circuit duplicate
calls."_ The api idempotency comment at `main.py:2587-2589` confirms the same contract. So a
**stable, delivery-derived trace_id is a required precondition** for the idempotency to work.

### Finding 3: user_gateway violates that contract with a random trace_id

**Evidence:** `services/user_gateway/app/message_router.py:64` — `trace_id = str(uuid.uuid4())`.

**Detail:** `_forward` generates a brand-new random trace_id on **every** call. It does not read the
Telethon `message.id`. There is no `update_id`-equivalent threaded through. There is also no
`user_gateway`-side dedup store (grep of `services/user_gateway/` for `dedup|idempoten|rate_limit|
spam` returns nothing), unlike the Bot-API path which has `webhook_dedup.py` + `rate_limit_
repository.py` + transcript `UNIQUE(source_message_id)`.

## Deduced Conclusions

### Deduction 1: a re-delivered Telethon message is fully re-processed

**Based on:** Findings 1 + 3.

**Reasoning:** Same customer message delivered twice → `_forward` runs twice → two **distinct**
random trace_ids → `find_by_trace_id` returns `None` both times → `claim_inbound` succeeds both times
→ the full handler body runs twice.

**Conclusion:** The dedup designed to protect against duplicate inbound (stories 12.24/12.40) is
**inert** on the `operator_user` channel. The bug is Confirmed at the mechanism level.

### Deduction 2: blast radius of one duplicate pass

**Based on:** the handler body after the claim (`main.py:2636+`) and the pipeline composition
(`main.py:696`).

**Reasoning / per-duplicate side effects:**
- **Customer reply sent twice** — the ack/answer outbound runs again (customer sees a duplicate).
- **LLM spend doubled** — the pipeline (OpenRouter call) re-runs; **compounded** by Epic-14
  cost-spike alerting (14-09) being unshipped, so nothing catches a burst (see Phase-2 report F-5).
- **HITL ticket** — if the duplicate escalates, a second ticket create+assign+operator-DM can fire
  (the in-flight `claim_inbound` only coalesces *same-trace_id* concurrency, which never happens
  here).
- **Sales-state mutation** — `SalesPersonaAnswerer` (`main.py:607`) mutates conversation state; a
  re-processed turn can advance/reset sales state incorrectly (e.g., re-trigger a counter-offer or
  reset a closing).

## Hypothesized Paths

### Hypothesis 1: Telethon re-delivers the same message often enough to matter in production

**Status:** Open

**Theory:** Telethon `events.NewMessage` can re-fire on reconnect / update-gap recovery /
multi-handler registration, producing a duplicate delivery of one logical message.

**Supporting indicators:** `operator_client_pool.py` maintains long-lived per-operator sessions
(reconnect-prone); no catch-up/seen-id guard exists in `message_router`.

**Would confirm:** a runtime log showing two `user_gateway_*` forwards (or two api `inbound_received`
with different trace_ids) for one Telethon `message.id`; or a Telethon-level test injecting a
duplicate `NewMessage` event.

**Would refute:** evidence that the deployed Telethon client config guarantees exactly-once delivery
(e.g., `sequential_updates` + a dedup the pool applies before `handle_new_message`). None found in
this scan.

**Resolution:** unresolved — magnitude link only. The **code defect is independent of this**; even a
single re-delivery triggers it.

## Missing Evidence

| Gap | Impact | How to Obtain |
| --- | --- | --- |
| Production logs of a re-delivery | Quantifies frequency → severity scaling | Add `message.id` to a `user_gateway_forward` log line; grep for repeats |
| Telethon client config (catch_up / sequential_updates) | Confirms/refutes Hypothesis 1 | Read `telegram_auth.py` / `operator_client_pool.py` client construction |

## Source Code Trace

| Element | Detail |
| --- | --- |
| Defect origin | `services/user_gateway/app/message_router.py:64` (`trace_id = str(uuid.uuid4())`) |
| Trigger | Any second delivery of a customer DM on a linked operator account |
| Condition | api idempotency keyed on `trace_id` (`main.py:2585-2634`) + non-stable trace_id from caller |
| Related files | `user_gateway/app/api_client.py` (forward_inbound), `bot_gateway/app/main.py:2491` (reference fix), `api/app/main.py:696` (pipeline side effects), `answer_trace` repo (claim/find) |

## Conclusion

**Confidence:** **High** for the defect (Confirmed root cause, deterministic mechanism, reference
implementation exists in the same repo). **Medium** for production *impact magnitude* (gated on
Hypothesis 1 — re-delivery frequency — which is unmeasured but plausible given long-lived reconnect-
prone sessions).

Root cause: `user_gateway` does not honor the repo's documented idempotency contract (a stable,
delivery-derived trace_id). It mints a random trace_id per delivery, so the api's two trace_id-keyed
dedup gates can never collapse a re-delivered MTProto message, and the message is re-processed with
full side effects.

## Recommended Next Steps

### Fix direction

Derive a deterministic trace_id in `message_router._forward` from the Telethon message identity, e.g.
`trace_id = f"tg-user-{operator_id}-{chat_id}-{message.id}"` (mirror `bot_gateway._derive_trace_id`).
This makes re-delivery collide on one trace_id and lets the existing api gates do their job — no api
change needed. Pair with A2: add a `user_gateway` rate-limit/spam gate before `queue.put_nowait`
(Epic-15 stories 15-04/15-05, currently untracked per Phase-2 F-3).

### Diagnostic

Add `message.id` to a structured `user_gateway_forward` log line; in staging, force a client
reconnect and confirm whether one logical message yields two `inbound_received` rows with different
trace_ids (confirms Hypothesis 1 and quantifies frequency).

## Reproduction Plan

1. **Isolated proof (no Telegram needed):** unit-test `MessageRouter._forward` — call it twice with
   the same fake `message` (same `message.id`/`chat_id`/`text`); assert `api_client.forward_inbound`
   is invoked with **two different** `trace_id` values. That alone demonstrates the idempotency
   bypass against the api contract (Findings 1-3).
2. **System repro:** with a linked operator session, send one customer DM, force a Telethon
   reconnect/gap so the message re-fires; observe two api `inbound_received` logs (distinct
   trace_ids) and a duplicate customer reply.

## Side Findings

- **A3 (MEDIUM, separate):** `calendar/oauth_state_repository.py:91` `consume()` is a non-atomic
  SELECT-then-UPDATE despite an "atomic single-use" docstring — concurrent same-`state` callbacks can
  both pass the unconsumed check. Unrelated to this case; tracked in the Phase-3 audit report.
- The api's `claim_inbound` (story 12.24) is a genuinely strong primitive — the gap is purely that
  the `user_gateway` caller never feeds it a stable key.

**Status:** Concluded — root cause Confirmed; Hypothesis 1 (frequency) left open as a magnitude
question, not a blocker.
