# Story 12.101: «На сколько человек рассчитан один багги?» defers to human, not echoes (round-28 D4)

Status: review

## Story
As a customer who asks «На сколько человек рассчитан один багги?», I want «Уточняю у коллег, какие варианты есть, и сразу сообщу» (capacity-defer copy), not the bot echoing my question back verbatim.

## Root cause (CONFIRMED — live regression, 2026-06-08)

`is_capacity_question(question)` (story 12-59 / 12-63) was designed to match headcount→buggy-count queries: «Сколько багги нужно на 8 человек?» ✅. The pattern requires «багг» keyword plus a headcount figure. «На сколько человек рассчитан один багги?» is the INVERSE question (capacity-per-buggy, not buggies-for-headcount) — it may or may not match depending on the exact regex.

Verified live (2026-06-08):
- «Сколько багги нужно на 8 человек?» → «Уточняю у коллег…» ✅ (is_capacity_question=True)
- «На сколько человек рассчитан один багги?» → bot echoed the question verbatim ❌

When `is_capacity_question` returns False, the message reaches the LLM extraction path. The LLM appears to have generated the question text back as its output — likely because with no matching service data and an ambiguous intent, it reproduced the input as a clarifying echo. This is an LLM output guardrail failure, but the root fix is making `is_capacity_question` cover the inverse formulation.

## Fix

Extend `is_capacity_question` to also match capacity-per-buggy questions: «на сколько человек», «сколько мест», «сколько пассажиров», «рассчитан на», «вмещает».

```python
_CAPACITY_INVERSE_RE = re.compile(
    r"на\s+скольк\w*\s+человек"   # «на сколько человек рассчитан»
    r"|скольк\w*\s+мест\w*"        # «сколько мест в багги»
    r"|скольк\w*\s+пассажир\w*"    # «сколько пассажиров»
    r"|рассчитан\s+на"             # «рассчитан на X»
    r"|вмещает\w*",                # «вмещает»
    re.IGNORECASE | re.UNICODE,
)
```

Both directions (how-many-buggies AND how-many-people-per-buggy) route to `_handle_capacity_question` which defers to human with `CAPACITY_ESCALATION_LINE` / `HITL_REASON_CAPACITY`.

## Acceptance Criteria
1. «На сколько человек рассчитан один багги?» → capacity-defer copy; never echoes the question. ✅
2. «Сколько мест в одном багги?» → same defer. ✅
3. «Сколько пассажиров вмещает багги?» → same defer. ✅
4. «Сколько багги нужно на 8 человек?» — unchanged (existing forward direction). ✅
5. Gates green; 100% coverage.

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`is_capacity_question`, `_CAPACITY_INVERSE_RE`)
- tests: `test_sales_persona_answerer_early_busy_check.py`
