# Story 12.87: Multi-option TIME «или» → per-time verdict (round-23 R23-2)

Status: review

## Story
As a customer who offers two times on one day («завтра в 12:00 или в 16:00»), I want an explicit per-time verdict so I know which time is free, not one unstated «свободно».

## Root cause (CONFIRMED)
The round-16 multi-option handler only covered «<день1> или <день2> в HH:MM» (two dates, one shared time) — it split on «или», required ≥2 distinct dates, and used `clocks[0]`. «завтра в 12:00 или в 16:00» has one date and two times → `len(dates) < 2` → it returned `None`, and the funnel produced a single verdict.

## Fix
`_maybe_answer_multi_date` now handles both shapes: ≥2 distinct dates → per-DAY verdict (shared time, «…Какой день вам удобнее?»); exactly 1 date + ≥2 distinct clocks → per-TIME verdict on that date («7 июня в 12:00 - свободно; 7 июня в 16:00 - свободно. В какое время вам удобнее?»). Shared slot-check + verdict-format; `None` when the calendar can't verify both.

## Acceptance Criteria
1. «завтра в 12:00 или в 16:00» → both times stated with a per-time verdict + «В какое время вам удобнее?». ✅
2. One time busy + one free → «занято; свободно». ✅
3. The existing two-dates-one-time per-day verdict is unchanged. ✅
4. Gates green; 100% coverage. ✅

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_maybe_answer_multi_date` — per-date + per-time branches)
- tests: `test_sales_persona_answerer_early_busy_check.py`

## Notes
- **R23-1 (price) — confirmed data-blocked, no code change.** Validated against production: the service's `price_text` is `None` and the live RAG has 0 price chunks, so no buggy price exists anywhere the bot can read. Per the AC ("only defer when the price genuinely isn't configured"), the current «Уточню у коллег и сразу сообщу» defer is already correct. The one latent code gap (the RAG-only `price_lookup` ignores the catalog `price_text` field) is left as-is per the round-23 decision; revisit when a real price is provided (alongside N2 capacity).
- **Y3 (R17-3 relative offset):** «через час» → now+1h now returns a verdict; locked with a busy-slot fixture (`…through_hour_busy…` → занято).
