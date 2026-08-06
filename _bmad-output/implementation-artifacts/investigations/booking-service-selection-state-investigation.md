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
