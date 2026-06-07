# Story 12.97: Guard — FAQ working-hours equal the engine window (round-27 R26-1 NON-BUG)

Status: review

## Story
As the team, I want a regression guard proving the hours FAQ matches the booking engine, so the recurring "the close is ~18:00" QA misinference is settled in code.

## Root cause (CONFIRMED — NON-BUG, 4th close-time misinference)
The QA reported R26-1 "still wrong": the FAQ says «08:00–21:00» but the engine rejects 20:30 as off-hours, inferred as a ~18:00 close. Validated against **live production data**:
- `project_services.working_hours` (read live via `docker cp`) = flat `{day: ["08:00","21:00"]}` for all 7 days, duration 60 min — **unchanged, still 21:00**.
- The FAQ (`_format_working_hours`) and the availability engine (`parse_service_rule` → `_parse_windows`) read the **same** `service_rule.working_hours` — they cannot disagree.
- Reproduced the engine boundary with the live config: 17:30 free, 19:00 free, **20:00 free** (last bookable start = close − duration), **20:30 off** (a 60-min ride would end 21:30 > 21:00 close), 21:00 off.

The QA tested only 17:30 + 20:30 and inferred 18:00; but 19:00/20:00 are free → the close is **21:00**. The FAQ «08:00–21:00» is correct. Per the user's round-27 decision: guard only, no copy change.

## Fix
No code change. Added `test_faq_hours_equal_engine_window_and_last_start_boundary`: asserts `_format_working_hours` == the engine window AND the start boundary (17:30/20:00 free, 20:30/21:00 off) via `compute_availability` on the live flat-shape rule — locking FAQ==engine and the close-time semantics.

## Acceptance Criteria
1. FAQ hours == engine window (both 08:00–21:00) on the production flat shape. ✅
2. Boundary asserted: 20:00 free (last start), 20:30 off (ride ends past close). ✅
3. Gates green; 100% coverage. ✅

## Files
- tests: `test_sales_persona_answerer_early_busy_check.py`

## Carried (data-blocked, unchanged)
- **R23-1 price** — needs the real per-buggy price (`project_services.price_text` + `price_lookup` wiring).
- **N2 capacity** — needs seats-per-buggy for a headcount→buggy-count recommendation.
- **B4** — bare far-future date («15 января») defers (effective-lookahead extends the freeBusy range; deferred).
