# Story 12.90: FAQ working hours read via the engine parser (round-26 R26-1)

Status: review

## Story
As a customer who asks «со скольки до скольки работаете?», I want the real hours («с 08:00 до 21:00»), not the garbled «с 0 до 8».

## Root cause (CONFIRMED)
`_format_working_hours` (rounds 20/25) assumed the **nested** shape `{day: [["08:00","21:00"]]}`, but production `project_services.working_hours` is the **flat** shape `{day: ["08:00","21:00"]}` (confirmed live). Iterating a flat value treats each "HH:MM" **string** as a window and takes its chars → `min("0","2")="0"`, `max("8","1")="8"` → «с 0 до 8». The test fixtures happened to use the nested shape, so it passed tests but garbled production. (The QA's "08:00–18:00" is itself wrong — the live close is 21:00.)

## Fix
`_format_working_hours` now parses each day's value with the availability engine's own `_parse_windows` (which accepts BOTH the flat `[start,end]` and nested `[[start,end],…]` shapes), so the FAQ reports the SAME hours the booking engine enforces — the AC's requirement. Malformed entries are skipped. Result: «Работаем с 08:00 до 21:00.».

## Acceptance Criteria
1. Flat shape `{day: ["08:00","21:00"]}` → ("08:00","21:00"); nested still works. ✅
2. The FAQ reports the engine's window (single source of truth via `_parse_windows`). ✅
3. Malformed working_hours → defer (no crash). ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_format_working_hours` → `_parse_windows`)
- tests: `test_sales_persona_answerer_early_busy_check.py` (flat-shape unit + flat-rule FAQ)
