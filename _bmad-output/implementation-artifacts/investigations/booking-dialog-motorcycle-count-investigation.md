# Investigation: motorcycle booking count reply falls to ScopeGuard

## Hand-off Brief

1. **What happened.** After selecting `Мотоциклы`, the customer answered the
   bot's headcount question with `3`, but received `Это не ко мне.`.
2. **Where the case stands.** Active; the existing numeric-scoping fix is
   present, but the motorcycle path must be traced to determine whether the
   reply reaches active scoping and which numeric field is pending.
3. **What's needed next.** Reproduce the exact service-selection sequence with
   the current answerer and inspect persisted state before changing routing.

## Case Info

| Field | Value |
| --- | --- |
| Ticket | N/A |
| Date opened | 2026-08-05 |
| Status | Active |
| System | Semantaix sales persona, local API/bot gateway, Python 3.11 |
| Evidence sources | User transcript, current source, focused tests, local SQLite state |

## Problem Statement

The customer reports that the bot asks `Сколько человек поедет?` after
`Мотоциклы`, then declines the valid numeric answer `3` as out of scope.

## Evidence Inventory

| Source | Status | Notes |
| --- | --- | --- |
| User transcript | Available | Exact failing sequence and customer-visible text |
| Sales answerer source | Available | Service-selection and numeric-scoping branches inspected |
| Focused reproduction | In Progress | Must cover `Мотоциклы` then `3` |
| Live SQLite state | Partial | Need identify the customer's chat id before relying on it |
| Production logs | Missing | No exact trace id supplied |

## Investigation Backlog

| # | Path to Explore | Priority | Status | Notes |
| --- | --- | --- | --- | --- |
| 1 | Reproduce service selection followed by numeric headcount | High | In Progress | Stronghold is the exact customer transcript |
| 2 | Inspect persisted stage and intent between turns | High | Open | Distinguishes state loss from numeric parsing |
| 3 | Add regression and fix the owning branch | High | Open | Only after root cause is confirmed |

## Confirmed Findings

### Finding 1: The reported customer-visible failure is a real booking-path defect

**Evidence:** User-provided transcript on 2026-08-05.

**Detail:** The bot itself asks for headcount, so a numeric answer must be
handled as a booking field rather than routed to a generic scope decline.

## Deduced Conclusions

None yet; source-level and runtime reproduction are still in progress.

## Hypothesized Paths

### Hypothesis 1: Motorcycle selection is not persisted as an active scoping state

**Status:** Open

**Theory:** The `Мотоциклы` turn may produce a reply that looks like the next
question but leave no state row, so `3` is processed as a fresh message.

**Supporting indicators:** The numeric fix only runs when a non-dormant state is
already present.

**Would confirm:** State repository is empty or not `scoping` after the service
selection turn.

**Would refute:** State is `scoping` with missing `headcount` immediately before
the `3` turn.

**Resolution:** Pending reproduction.

## Missing Evidence

| Gap | Impact | How to Obtain |
| --- | --- | --- |
| Telegram chat id / trace id | Prevents matching the exact live SQLite state and logs | Read bot/API logs or provide the chat id |

## Source Code Trace

| Element | Detail |
| --- | --- |
| Error origin | To be confirmed; likely `SalesPersonaAnswerer._dispatch` / scoping gate |
| Trigger | `Мотоциклы` followed by bare numeric headcount |
| Condition | Must identify whether state is absent, wrong stage, or numeric branch skipped |
| Related files | `services/api/app/sales/sales_persona_answerer.py`, state repository, sales aliases |

## Conclusion

**Confidence:** Low until the exact state transition is reproduced.

The transcript confirms the customer-facing defect, but not yet whether the
root cause is motorcycle service recognition, state persistence, or a mismatch
between the asked field and the numeric parser.

## Recommended Next Steps

### Fix direction

Pending confirmed root cause.

### Diagnostic

Run a deterministic two-turn reproduction and assert the persisted state after
`Мотоциклы` before sending `3`.

## Reproduction Plan

1. Start with a fresh state repository.
2. Send `Мотоциклы` and record the result plus persisted stage/intent.
3. Send `3` with the same chat id.
4. Expect a handled scoping response and `headcount == 3`, never a ScopeGuard decline.

## Side Findings

- The existing numeric branch is already covered for an active scoping state
  and a temporary OpenRouter failure; this case tests whether motorcycle
  selection reaches that state.

## Follow-up: 2026-08-05

### New Evidence

- `matched_service_groups("Мотоциклы")` resolves to the offered `мопед`
  service family, and `_is_standalone_service_selection` returns `True` in the
  current source (`services/api/app/sales/service_aliases.py:37-38`,
  `services/api/app/sales/sales_persona_answerer.py:795-805`).
- The exact regression `Привет → Мотоциклы → 3` passes in
  `tests/test_sales_persona_answerer_scoping_reask_guard.py:141-157`.
- The old live process log for the reported `3` turn shows OpenRouter
  `402 Payment Required`, then `sales_llm_transport_error`,
  `handled=false`, and ScopeGuard decline. The old uvicorn processes had been
  started without `--reload` before the deterministic numeric fix.
- API and bot gateway were restarted with the current source as PIDs 63560 and
  63561; both local health endpoints and the existing ngrok endpoint returned
  `status=ok`.

### Additional Findings

The motorcycle service path and persisted scoping state were not the defect.
The customer-facing failure was caused by stale running processes combined
with the OpenRouter payment failure; the current source no longer depends on
OpenRouter for a numeric answer to a pending numeric field.

### Updated Hypotheses

- Hypothesis 1 — **Refuted**: motorcycle selection was not lost. The current
  resolver enters `service_selection` and persists `scoping`.
- Hypothesis 2 — **Confirmed**: the live process did not contain the latest
  deterministic numeric branch, so an OpenRouter `402` exposed the old
  fall-through behavior.

### Backlog Changes

- Exact reproduction: Done.
- State-path investigation: Done.
- Fix and live process restart: Done.

### Updated Conclusion

**Confidence: High.** The source fix was already correct, but the running API
process had not been restarted after it was applied. The exact motorcycle
sequence now captures `headcount=3` before any LLM call, including when
OpenRouter is unavailable. The local API, bot gateway, and existing ngrok
tunnel are live on the updated process.

### Live end-to-end confirmation

On 2026-08-05, a `3` update was sent through the existing public webhook. The
live API logged `sales_turn_kind=deterministic_numeric_reply` with
`field=headcount`, returned HTTP 200, sent the customer response successfully,
and persisted `headcount=3` for chat `5658965359`. This confirms the fix across
Telegram webhook, ngrok, bot gateway, API, and SQLite state.

## Follow-up: 2026-08-06

### New Evidence

- The user confirmed that the reported transcripts are from `@semantaix_bot`.
  Their timestamps match the local historical updates, so they are not a
  second-bot explanation.
- The old local API logs show the exact failure: the answerer asked for a
  numeric field, OpenRouter returned `402 Payment Required`, the old process
  fell through with `handled=false`, and ScopeGuard sent `С этим не ко мне.`.
- Six approved RAG sources are currently indexed for project 1 with 459
  chunks. The vehicle offerings found in them are квадроциклы, багги and
  эндуро/мотоциклы, plus related options such as скутер-туры, пикник and баня.
- A generic catalog question could also fail when the catalog digest was
  unavailable: keyword retrieval for `услуги/варианты` returned zero chunks
  even though the source documents contained the offerings.

### Updated Conclusion

The reported defect was real in the running local instance. The root cause was
the stale API process combined with an OpenRouter payment failure; the source
fix for deterministic numeric replies was not loaded by that process. A
second independent issue was the catalog fallback using a generic keyword
query instead of enumerating public RAG chunks when the digest was missing.

Both paths are now covered by automated tests and fixed. The API and bot
gateway have been restarted with the current source; the existing ngrok tunnel
continues to answer health checks. The RAG-driven E2E cases cover the exact
`Привет → услуга → завтра → 2/3` regression and complete all three vehicle
flows through a `sales_escalation` ticket assigned to `@flexsentlabs`.
