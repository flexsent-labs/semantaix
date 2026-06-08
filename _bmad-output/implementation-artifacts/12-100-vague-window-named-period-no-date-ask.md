# Story 12.100: «Во второй половине дня» proposes a slot, not a generic time-ask (round-28 D3)

Status: review

## Story
As a customer who says «Хочу забронировать багги завтра во второй половине дня, двоих», I want the bot to propose a concrete free slot from the 12:00–18:00 window (e.g. «Есть свободное время, например в 15:00»), not ask «Уточните, пожалуйста, желаемое время».

## Root cause (CONFIRMED — live regression, 2026-06-08)

`_maybe_answer_vague_window` (line 3202+) calls `detect_vague_window(question)` which should return the `(12, 18)` window for «во второй половине дня». If that succeeds, it then calls `extract_requested_date` to get a `target_date` (line 3224-3228). The function returns `None` (no slot proposed) when `target_date is None` (line 3229-3230).

When the LLM failed to extract «завтра» into `intent.dates` (leaving it `None`), `dates_text` is `None`. The fallback `extract_requested_date(text=question, ...)` is called with the full question string «Нет. Хочу забронировать багги завтра во второй половине дня, двоих». This should resolve «завтра» to tomorrow's date — but may fail if the «Нет.» prefix or a mismatch between `detect_vague_window` and `extract_requested_date` context causes one to succeed where the other does not.

Likely secondary issue: `_has_time_without_date` fires for «во второй половине дня» if `extract_all_clocks` matches the time-phrase as a clock AND `extract_requested_date` returns None. If `_has_time_without_date` fires BEFORE `_maybe_answer_vague_window` completes its date lookup, the generic ask-for-time preempts the slot proposal.

Note: «после 15:00» (test 3.15) correctly triggered the vague-bound path because `extract_time_bound` handles it and `detect_vague_window` converts it to a window. The difference suggests the NAMED-PERIOD branch («во второй половине дня») has a specific failure — perhaps a regex anchor or ordering issue.

Verified live (2026-06-08):
- «Хочу завтра во второй половине дня, двоих» → «Уточните, пожалуйста, желаемое время» ❌
- «Хочу завтра после 15:00, нас двое» → «Есть свободное время, например в 15:00» ✅

## Fix

1. Reproduce in a unit test: call `detect_vague_window("Нет. Хочу забронировать багги завтра во второй половине дня, двоих")` and assert it returns `(12, 18)`. If it returns `None`, the named-period regex doesn't match in context → fix the regex (add `re.UNICODE`, word-boundary, or extend the pattern).

2. Ensure `_maybe_answer_vague_window` is called with the raw `question` (not `intent.dates`) when `intent.dates` is None, and that `extract_requested_date(question)` resolves «завтра» correctly.

3. If the issue is `_has_time_without_date` short-circuiting (extracting a clock from the vague period phrase), add a guard: if `detect_vague_window(question)` is not None → return `False` from `_has_time_without_date` (a vague window is NOT a time-without-date case).

## Acceptance Criteria
1. «Хочу завтра во второй половине дня, двоих» → concrete slot proposed from 12–18 window. ✅
2. «Хочу завтра утром, нас двое» → slot from 08–12 window. ✅
3. «Хочу завтра вечером, нас двое» → slot from 16–20 window. ✅
4. «Хочу завтра после 15:00» — unchanged (bound path still works). ✅
5. Gates green; 100% coverage.

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_has_time_without_date` vague-window guard, `_maybe_answer_vague_window` fallback)
- tests: `test_sales_persona_answerer_early_busy_check.py`
