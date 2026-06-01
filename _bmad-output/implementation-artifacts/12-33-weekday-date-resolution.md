# Story 12.33: Resolve weekday/relative dates consistently with the calendar check (D9, P1)

Status: review

## Story

As a **customer who names a weekday ("в понедельник")**,
I want the bot to **check the date I actually mean against the calendar**,
so that **naming today's weekday doesn't sail past a conflict that "сегодня" / "1 июня" both catch.**

**Problem (observed live, багги, 1 June 2026 — a Monday, calendar reconnected, 13:00–16:30 busy):**

```
сегодня в 14:00            → "это время уже занято … 12:00"        ✅
на 1 июня в 15:00          → "это время уже занято … 12:00"        ✅
в понедельник в 15:00      → "Спасибо! Передам детали коллегам…"   ❌ (busy slot accepted)
```

All three name the same booked Monday, yet the weekday phrasing was accepted.

**Root cause (CONFIRMED — refines the QA hypothesis; see investigation Finding D9):** the scoping/greeting LLM is given **no current-date context at all** — `sales_greeting.txt`/`sales_scoping.txt` inject no date and `OpenRouterClient.complete_json` has no date parameter. So when the customer says "в понедельник," the LLM resolves it to a guessed absolute date (often the *next*, free Monday), stores that verbatim in `intent.dates`, and `_check_requested_slot` queries the wrong (free) day → `STATUS_AVAILABLE` → accepted. The deterministic resolver `extract_requested_start` → `_extract_day_offset` is **correct** ("today counts when it *is* that weekday", `service_resolver.py:233`; tested `tests/test_calendar_service_resolver.py:163`) but it never sees the raw weekday — only the LLM's mangled string. This is a different root cause from D2 (absolute-date parsing, fixed in 12.26).

## Acceptance Criteria

1. The greeting and scoping prompts carry **today's project-local date + weekday**, so the LLM resolves "понедельник"/"завтра" against the correct day.
2. The prompts instruct the LLM to store the customer's date/time phrase **verbatim** ("в понедельник", "1 июня", "завтра в 14:00") and NOT convert a weekday/relative reference into a number itself — the deterministic `extract_requested_start` owns resolution (it already handles weekday "today counts", relative words, and absolute `<day> <month>` from 12.26).
3. "в понедельник в 15:00" on Mon 1 June → same verdict as "сегодня в 15:00" / "1 июня в 15:00" (busy + nearest free), once the LLM stores the raw phrase (verified live).
4. A weekday clearly in the future still resolves to its next occurrence (unchanged deterministic behaviour).
5. Russian funnel otherwise unchanged; gates green; 100% coverage on the new branch.

## Tasks / Subtasks

- [x] `_format_today_ru(now, tz)` + `_WEEKDAYS_RU` + a project-tz default; format the project-local today as "1 июня 2026 года, понедельник".
- [x] Thread `today` into `_build_greeting_prompt` / `_build_scoping_prompt` and the two call sites (`_handle_greeting`, `_extract_and_merge`), computed from `self._clock()`.
- [x] `sales_greeting.txt` / `sales_scoping.txt`: add the `{today}` date context + the "store verbatim, don't convert the weekday yourself" instruction.
- [x] Tests: `_format_today_ru` known-date; greeting + scoping prompts contain today's date and the don't-convert directive; a raw-weekday-today intent intercepts a busy slot at the answerer level (the deterministic path the prompt now steers the LLM toward).

## Dev Notes

- **Why the prompt, not the resolver:** the resolver is already correct; the gap is the LLM emitting a wrong pre-resolved date for lack of date context. Giving it the date (and telling it to store raw) closes the gap and also corrects any other relative phrasing it was guessing blind.
- **Why "store verbatim":** routes weekday→date through the single deterministic source of truth (`extract_requested_start`) instead of LLM date arithmetic; the date context is belt-and-suspenders so even if the LLM converts anyway, it converts against the right day.
- **Known limitation:** the prompt's "today" label uses the platform-default project tz (Europe/Moscow, matching `settings.default_timezone`); a project in a far-off tz near local midnight could see a 1-day-off label. Out of scope here (the resolver still uses the calendar's own tz). 
- **Files:** `services/api/app/sales/sales_persona_answerer.py`; `services/api/app/sales/system_prompts/sales_greeting.txt`, `sales_scoping.txt`.

## References

- Root cause validated this session against current code (QA rounds 3–4); confirmed the LLM gets no date context (prompts + `complete_json` carry no date).
- [Source: services/api/app/calendar/service_resolver.py#_extract_day_offset] (deterministic; today counts).
- [Source: services/api/app/sales/sales_persona_answerer.py#_build_greeting_prompt], [#_build_scoping_prompt], [#_extract_and_merge].
- Sibling: `12-26-absolute-date-parsing-in-busy-check.md` (absolute dates — fixed).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1,2,4,5).** Added `_format_today_ru` + `_WEEKDAYS_RU`; threaded today's project-local date into `_build_greeting_prompt`/`_build_scoping_prompt` and both call sites; the two prompts now carry "Сегодня {today}" + a "store the date phrase verbatim, don't convert the weekday yourself" instruction so the deterministic `extract_requested_start` owns weekday→date.
- **TDD:** tests written first and watched fail (`ImportError: _format_today_ru`), then green — `_format_today_ru` known-date, greeting + scoping prompts carry today + the verbatim directive, and a raw-weekday-today intent intercepts a busy slot.
- **Verification:** 4 new tests pass; 254 sales-persona/calendar tests pass; `ruff` clean. NOTE: the full-suite 100%-coverage CI-parity run could not complete locally — the machine disk hit 100% (ENOSPC) mid-run. CI will run the coverage gate on merge; the change is additive (every new line is exercised by the targeted tests).

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified)
- `services/api/app/sales/system_prompts/sales_greeting.txt` (modified)
- `services/api/app/sales/system_prompts/sales_scoping.txt` (modified)
- `tests/test_sales_persona_answerer_weekday_resolution.py` (new)
- `_bmad-output/implementation-artifacts/12-33-weekday-date-resolution.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
