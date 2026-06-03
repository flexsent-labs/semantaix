# Story 12.54: Mixed-intent turns get a one-line out-of-scope decline (round-12 D5)

Status: review

## Story

As a **customer who asks for something off-topic AND books in the same message** («Посоветуйте ресторан и заодно запишите на багги завтра в 10:00»), I want **the booking handled and the off-topic part briefly declined**, so that **my restaurant ask isn't silently ignored.**

**Problem (live, round 12):** the booking was correctly confirmed (свободно) but the restaurant request was dropped with no acknowledgement.

## Root cause (CONFIRMED)

The out-of-scope decline in `_dispatch` is gated on `is_out_of_scope(q) and not is_sales_intent(q)` (Story 12.34) — deliberately, so a mixed message stays in the funnel and a field answer is never swallowed. But that means when a message carries BOTH intents, only the booking is handled; nothing tells the customer the off-topic part won't be done. Confirmed: `is_out_of_scope` + `is_sales_intent` both return True for the mixed phrase, and only the funnel reply is returned.

## Fix

After `_dispatch` returns in `try_answer`, when the turn was **handled** and the message carried **both** an out-of-scope ask and a sales intent, append a one-line decline (`MIXED_OUT_OF_SCOPE_SUFFIX`, localized) to the reply. It's a short "А с остальным, к сожалению, не помогу — я по прокату багги." — distinct from the full `OUT_OF_SCOPE_DECLINE_LINE` (which re-asks dates/headcount and would duplicate the funnel). A pure booking never matches `is_out_of_scope`, so this never fires on a normal booking.

## Acceptance Criteria

1. A mixed booking + out-of-scope ask → the booking is handled AND a one-line decline is appended. ✅
2. A pure booking gets no decline suffix. ✅
3. Works regardless of which booking line the funnel produced (busy verdict / free handoff / scoping question). ✅ (appended at the `try_answer` seam)
4. Mirrors the turn's language (RU/EN). ✅
5. Gates green; 100% coverage. ✅

## Tasks / Subtasks

- [x] `MIXED_OUT_OF_SCOPE_SUFFIX` (+ `_EN`); append in `try_answer` gated on handled + both intents.
- [x] Tests (TDD): mixed booking → suffix present (alongside the busy verdict); pure booking → no suffix.

## Dev Notes

- The gate `is_out_of_scope AND is_sales_intent` is the precise "mixed" signal; the existing pure-out-of-scope path (`AND not is_sales_intent`) is unchanged, and cancellation (routed first) isn't a sales intent so it never triggers the suffix.
- Appending at `try_answer` keeps the funnel handlers untouched (no signature churn) and covers every booking-reply shape.
- **Files:** `services/api/app/sales/sales_persona_answerer.py`.

## References

- Round-12 live QA, "Minor: mixed-intent out-of-scope part dropped". Related: Story 12.34 (out-of-scope decline).
- [Source: sales_persona_answerer.py#try_answer], [#MIXED_OUT_OF_SCOPE_SUFFIX].

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–5).** A mixed booking + off-topic message now books AND declines the off-topic part in one line, language-mirrored; pure bookings are unaffected. TDD; full suite green at 100% coverage.

### File List

- `services/api/app/sales/sales_persona_answerer.py` (modified — `MIXED_OUT_OF_SCOPE_SUFFIX` + `try_answer` append)
- `tests/test_sales_persona_answerer_early_busy_check.py` (modified — mixed/pure tests)
- `_bmad-output/implementation-artifacts/12-54-mixed-intent-out-of-scope-decline.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
