# Investigation: Service selection reused stale booking fields

## Hand-off Brief

1. **What happened.** After `квадро` the bot asked only for the vehicle count and handed the request off after `3`, without collecting date and party size for the new service.
2. **Where the case stands.** The root cause is confirmed: standalone service selection preserved the previous conversation's `Intent`, so old date/headcount values made scoping appear complete.
3. **What's needed next.** Reset booking fields whenever a customer selects a standalone service, then verify the fresh flow for a multi-person service with the LLM unavailable.

## Case Info

| Field | Value |
| --- | --- |
| Ticket | N/A |
| Date opened | 2026-08-06 |
| Status | Resolved |
| System | Local API + Telegram bot gateway + ngrok, Python 3.11 |
| Evidence sources | Telegram/API runtime logs, SQLite sales state, source code, tests |

## Problem Statement

The user expects every service that can involve multiple people to collect the date and party size before vehicle count and final handoff.

## Evidence Inventory

| Source | Status | Notes |
| --- | --- | --- |
| User transcript | Available | `квадро` → `Сколько квадроциклов нужно?` → `3` → immediate operator handoff. |
| API runtime logs | Available | Update `726743234` handled as service selection; update `726743235` completed scoping. |
| Sales SQLite state | Available | Before the final turn the conversation retained prior fields; after it state contained `dates=завтра`, `headcount=3`, `vehicle_count=3`. |
| Source code | Available | `_handle_service_selection` starts from the stored `Intent` and adds only the new service. |
| OpenRouter | Available | The observed scoping call returned HTTP 402; deterministic fallback must remain usable. |

## Investigation Backlog

| # | Path to Explore | Priority | Status | Notes |
| - | --- | --- | --- | --- |
| 1 | Trace the observed turns through state and logs | High | Done | State reuse and completion are confirmed. |
| 2 | Reset stale fields on standalone service selection | High | Done | Implemented and covered from scoping, pitching, and complete prior states. |
| 3 | Verify date → headcount → vehicle-count flow without LLM | High | Done | Deterministic temporal/numeric fallback path is covered by the full dialogue test. |

## Timeline of Events

| Time | Event | Source | Confidence |
| --- | --- | --- | --- |
| 2026-08-06 16:55:24 | `квадро` handled as `service_selection` while state stage was `scoping`. | API runtime log | Confirmed |
| 2026-08-06 16:55:36 | Reply `3` reached `scoping_complete` and was escalated. | API runtime log | Confirmed |
| 2026-08-06 | State row retained `dates=завтра`, `headcount=3`, then added `vehicle_count=3`. | `.data/semantaix_sales.db` | Confirmed |

## Confirmed Findings

### Finding 1: Standalone service selection preserves stale fields

**Evidence:** `services/api/app/sales/sales_persona_answerer.py:2287-2295` builds the new service selection from `Intent.from_dict(state["collected_intent"])` and only overwrites `service`; `:2295-2300` then calculates missing fields from that reused intent.

**Detail:** A new service word is a new booking request, but the implementation treated it as a continuation of the old service's questionnaire. Existing date/headcount values therefore suppress the questions the user expects.

### Finding 2: The handoff was a deterministic consequence of stale completeness

**Evidence:** The API logged `sales_turn_kind=scoping_complete` for the `3` turn, and the persisted state contained all three configured required fields. The same turn also logged an OpenRouter HTTP 402, so the result came through the fallback path rather than a successful extraction.

**Detail:** The LLM outage exposed the stale-state bug but did not create it; with a clean intent, the deterministic numeric fallback would ask the next missing field instead of completing the booking.

## Deduced Conclusions

### Deduction 1: The required-field schema was not the primary defect

**Based on:** Findings 1 and 2.

**Reasoning:** The default/fallback booking schema includes `dates`, `headcount`, and `vehicle_count`. They were already present in state before the new service was selected, so the schema had no missing field to ask.

**Conclusion:** Resetting the intent at the service-selection boundary restores the required sequence for all multi-person services.

## Hypothesized Paths

### Hypothesis 1: The service-specific schema intentionally omits date or headcount

**Status:** Refuted for the observed project.

**Would confirm:** A project/service schema row with those fields not required.

**Resolution:** The local project has no custom `scoping_schemas` row; fallback configuration is the three-field booking set. The observed skip came from reused values.

## Missing Evidence

| Gap | Impact | How to Obtain |
| --- | --- | --- |
| A fresh live Telegram turn after the fix | Confirms the user-visible question order | Send `квадро`, then a date, party size, and vehicle count. |

## Source Code Trace

| Element | Detail |
| --- | --- |
| Error origin | `services/api/app/sales/sales_persona_answerer.py:2287-2295` |
| Trigger | A one-word offered-service selection such as `квадро` or `багги`. |
| Condition | Existing state has prior booking fields. |
| Related files | `sales/intent.py`, `sales/scoping_schema.py`, `tests/e2e/test_e2e_customer_service_dialogues.py`. |

## Conclusion

**Confidence:** High

The bot did not skip date and party size because those requirements were absent. It reused values from the previous booking state when a new standalone service was selected. The fix is to retain only the newly selected service and restart scoping from the first required field.

## Recommended Next Steps

### Fix direction

Reset the `Intent` to a fresh instance containing only the selected service and allow standalone service selection to restart scoping from any existing stage.

## Resolution and Verification

The implementation now treats a standalone service name as the start of a new
booking. It stores only the selected service, resets the previous date, party
size, vehicle count, and optional fields, and asks the first required question
again. The standalone-service dispatch is no longer limited to `new` and
`scoping`, so stale `pitching`/`closing` state cannot bypass the questionnaire.

The regression flow was verified without a working LLM transport:

```text
Мотоциклы → На какую дату планируете? → завтра → Сколько человек поедет? → 3
```

Verification results:

- focused sales/E2E regression: 20 passed;
- broader sales regression: 37 passed;
- full suite with the repository's required coverage gate: 4158 passed,
  100.00% coverage.

The observed OpenRouter HTTP 402 remains a separate infrastructure/dependency
issue. It no longer prevents terse date or numeric answers from advancing the
conversation because the deterministic fallback path is covered.

### Diagnostic

Exercise the same service-selection flow with a failing LLM: expect date, then headcount, then vehicle count, and no completion before all three are supplied.

## Reproduction Plan

Seed a scoping/pitching state with `dates` and `headcount`, send a standalone `квадро`, and assert the answer asks for the date and the persisted intent contains only `service=квадроцикл`. Continue with `завтра`, `нас трое`, and `3`; assert the final handoff occurs only after all required fields exist.

## Follow-up: 2026-08-06 #2

### New User-Reported Regression

After the neutral greeting, the customer sent `хочу покататься завтра`. The bot
immediately returned the booking handoff instead of asking which service,
party size, and other required details were needed.

### Confirmed Evidence

- The local API log for trace `tg-update-726743237` records the message at
  `2026-08-06T23:45:18` with `text: "хочу покататься завтра"`.
- The same trace records `stage_before: "pitching"`,
  `stage_after: "pitching"`, `sales_turn_kind: "scoping_complete"`, and
  `hitl_reason: "sales_scoping_complete"`.
- The persisted sales row after the turn contained stale booking fields:
  `service: "квадроцикл"`, `headcount: 3`, and `vehicle_count: 3`, in addition
  to the new date text. These fields were not supplied by the reported message.
- The source dispatch sends every `pitching` turn to `_handle_pitching`
  (`services/api/app/sales/sales_persona_answerer.py:1854-1856`). That handler
  treats a date-bearing turn as a counter-offer/completion path and does not
  detect a fresh booking opener when no new service is named.
- The existing service-selection fix covers a standalone offered-service word
  such as `квадро`, but not a generic fresh booking opener such as `хочу
  покататься завтра`.

### Root Cause

**Confirmed:** the `pitching` state is treated as a continuation of the prior
booking. A fresh booking request that contains an activity and date but no
explicit service therefore reuses the previous `Intent`; the stale service,
headcount, and vehicle count make the completion gate believe that all required
fields are present and route directly to HITL.

The schema is not the cause. The reported message has no service or party size,
but the state carried both from the previous booking.

### Fix Direction

Before `_handle_pitching` interprets a date as a counter-offer, detect a fresh
booking opener with no active offered slot and re-enter the new-booking greeting
path with a clean `Intent`. That path should preserve the newly stated date,
then ask for the missing service and party size instead of completing from stale
state. Add a regression for `pitching` after a prior handoff with
`хочу покататься завтра`, plus negative cases for a genuine offered-slot
counter-offer and a plain closure such as `всё понятно`.

### Status

Diagnosis concluded; implementation is pending explicit approval because this
changes the pitching state-machine semantics.

## Follow-up: 2026-08-06 #3

### New User-Reported Regression

The customer asked `каки есть квадрациклы` and then `а какие багги?`. The first
turn was sent directly to the operator. The second was answered with the
out-of-scope line `Не моя тема 🙂` even though the knowledge base contains
multiple buggy entries.

### Confirmed Evidence

- For `каки есть квадрациклы` (trace `tg-update-726743238`), the API log shows
  `stage_before: "pitching"` and `sales_turn_kind: "pitching_followup"`; the
  catalog classifier returned no catalog intent, so the pitching continuation
  handler treated the question as a booking follow-up and escalated it.
- `services/api/app/sales/turn_intent.py:48-61` recognizes only fixed catalog
  phrases such as `какие туры`, `услуги`, `варианты`, and `что у вас есть`.
  Neither `какие есть квадрациклы` nor the typo `каки есть квадрациклы` is a
  catalog phrase. The live normalizer also produces `каки` rather than the
  expected `какой`, so the typo is not repaired before classification.
- For `а какие багги?` (trace `tg-update-726743239`), RAG retrieved 24 matches
  and returned three chunks with top score `1.0`, including
  `Квадроциклы, Багги и Эндуро.`, `Багги (BRP & Yamaha) Эндуро (Kayo & GR 8)`,
  and a buggy pricing chunk. The same content is persisted in
  `.data/semantaix_rag.db` rows `3`, `15`, `59`, `135`, and many rows from
  `knowledge_candidate:5`.
- The RAG generation call for that exact question returned HTTP `402 Payment
  Required` at `2026-08-06T23:46:08`. The preceding sales LLM call also
  returned 402. Model discovery returning 200 at startup does not prove that
  chat completions are billable for the loaded local key.
- The follow-up `какие есть мотоциклы?` (trace `tg-update-726743240`) follows
  the same path: RAG returns three score-`1.0` chunks, including training on a
  motorcycle/enduro and a description of light, powerful motorcycles, then the
  grounded generation call returns HTTP `402` and the customer receives
  `Не смогу тут помочь.`
- `GroundedRagAnswerer` has a transport fallback only when the broad
  `catalog_query` detector fires (`services/api/app/answerers/grounded_rag.py:277-301`).
  A specific question about `багги` is not classified by that detector, so the
  retrieved RAG chunks are discarded when generation fails and the pipeline
  falls through to `Не моя тема 🙂`.

### Updated Diagnosis

This is a three-part defect, not missing knowledge:

1. generic service-specific catalog questions are absent from the sales
   classifier, and common typos are not normalized for that intent;
2. the `closing`/`pitching` state can route an informational question into the
   booking continuation instead of preserving it as a knowledge question; and
3. when OpenRouter chat completion is unavailable, the RAG answerer cannot
   return the already-retrieved factual chunks for a specific service query.

### Fix Direction

Add a typo-tolerant service-specific catalog intent (for example, `какие есть
<услуга>` / `каки есть <услуга>`), intercept it in all active post-handoff
states, and use the bounded RAG excerpt fallback for high-confidence specific
service questions when the generator is unavailable. Separately verify the
OpenRouter account/key used by the local `.env`, because the live `402` is an
independent infrastructure blocker for normal grounded prose.

### Status

Root cause confirmed; no application code changed in this follow-up.
