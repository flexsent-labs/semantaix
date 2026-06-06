# Round-24: R24-1 non-bug verification + regression guards

Status: review (test-only; no production code change)

## Summary
Live QA round 24 verified **R23-3 fixed** ✅ and flagged one "new defect" (R24-1). Investigation against production confirms **R24-1 is a non-bug** — the same root cause as R21-3: the QA harness assumes a **~18:00 close, but the live config is 08:00–21:00**. This round adds regression guards only.

## R24-1 (P2, reported) — «прямо сейчас» after closing → свободно: CONFIRMED NON-BUG
The QA expected «прямо сейчас» at 19:11 → off-hours, hypothesising either «сейчас» isn't resolved to now, or the off-hours/past guards are skipped for now-relative times. **Both are false**, proven against production:
- Live `project_services` for «Аренда багги»: hours **08:00–21:00**, 60-min slots (re-read live).
- «прямо сейчас» **resolves to now** (round-18/12.75) AND the guards **apply**:
  - 19:11 → ride 19:11–20:11 < 21:00 → **free** (correct — the QA's 18:00 close is wrong)
  - 20:30 → ride ends 21:30 > 21:00 → off-hours
  - 22:00 → off-hours; 06:30 → before open → off-hours
- The this-round positives corroborate: «6 утра»→off-hours (before open) and «9 вечера»=21:00→off-hours (R21) are both correct at the 21:00 close.

No code change — rejecting 19:11 would break valid 18:00–20:00 «сейчас» bookings. (This is the **second** false report from the 18:00-close assumption; the QA harness's "Hours ~08:00–18:00" note should be corrected to **08:00–21:00**.)

## Guards added (test-only)
- **Z1** (`test_requested_time_check.py::test_now_instant_goes_through_offhours_and_busy_guards`): a now-instant («сейчас») → off-hours after close (22:00), free in-hours (19:00), занято when the slot is busy. Locks R24-1.
- **Z2** (`test_calendar_service_resolver.py::test_slang_service_word_does_not_block_date_parse`): «на квадриках 9 июня в 12:00» → 9 June 12:00 (slang service word doesn't block the parse; verdict is global-calendar-driven).
- **Z3** (`test_calendar_service_resolver.py::test_early_morning_six_am_resolves`): «6 утра» → 06:00 (digit+«утра», lower off-hours bound).
- R23-3 multi-turn re-check + R21-3 closing boundary remain guarded from prior rounds.

## Still open (carried, both data-blocked)
- **R23-1 price** — `price_text` is None and the RAG has 0 price chunks; defer is correct until a real price is provided.
- **N2 capacity** — needs the real seats-per-buggy figure.

## Gate
`ruff` clean; 3400 passed; 100.00% coverage.
