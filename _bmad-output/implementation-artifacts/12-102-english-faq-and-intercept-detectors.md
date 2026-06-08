# Story 12.102: English FAQ and intercept phrases trigger correct handlers, not scoping (round-28 D5)

Status: review

## Story
As an English-speaking customer who asks «Are there any discounts?», «What time do you open?», or «I'd like to talk to a real person», I want the same correct handler that Russian phrases receive (FAQ defer, hours answer, human-handoff) — not the scoping funnel asking «What date are you planning for?».

## Root cause (CONFIRMED — live regression, 2026-06-08)

All deterministic FAQ and intercept detectors use Russian-only regex patterns:

| Detector | Russian pattern | English gap |
|----------|----------------|-------------|
| `_HUMAN_REQUEST_RE` | «живым человеком», «менеджера», «оператором» | «real person», «talk to a human», «speak to a manager» not matched |
| `_DISCOUNT_RE` | «скидки», «акции», «промокод» | «discount», «promo code», «special offer» not matched |
| `is_working_hours_question` | `_SCHEDULE_RE` + `_HOURS_QUESTION_RE` (Russian) | «what time do you open/close», «working hours» not matched |
| `is_working_days_question` | `_DAYS_QUESTION_RE` (Russian) | «do you work on sundays», «what days are you open» not matched |
| `is_info_faq_question` (`_PAYMENT_RE`, `_LOCATION_RE`, `_BRING_RE`) | Russian only | «pay by card», «where are you located», «what to bring» not matched |

English messages that reach these detectors return False → fall through to `is_sales_intent` → LLM scoping funnel → bot asks «What date are you planning for?» instead of giving the correct FAQ/handoff response.

Verified live (2026-06-08):
- «Are there any discounts?» → «What date are you planning for?» ❌
- «What time do you open?» → «What date are you planning for?» ❌
- «I'd like to talk to a real person» → «What date are you planning for?» ❌
- «Hello! I want to rent a buggy» → «What date are you planning for?» ✅ (English routing works for booking flow)

## Fix

Add English alternatives to each detector regex (appended with `|`, IGNORECASE already set):

**`_HUMAN_REQUEST_RE`** — add:
```
|(?:real|actual|live|human)\s+(?:person|agent|human)
|\bspeak\s+(?:to|with)\s+(?:a\s+)?(?:person|human|agent|manager|representative|operator)
|\btalk\s+(?:to|with)\s+(?:a\s+)?(?:person|human|manager|agent|representative)
|\bconnect\s+(?:me\s+)?(?:to|with)\s+(?:a\s+)?(?:person|human|agent|manager)
|\bnot\s+(?:a\s+)?bot\b|\bno\s+bot\b
```

**`_DISCOUNT_RE`** — add:
```
|discount\w*|promo\s*code\w*|coupon\w*|special\s+offer\w*|deal\w*
```

**`_SCHEDULE_RE` / `is_working_hours_question`** — add English pattern:
```
|what\s+(?:time|hour)\w*\s+(?:do\s+you\s+)?(?:open|close|work|operate)
|(?:opening|closing|working)\s+hours?
|(?:open|close)\s+(?:at|until|from)
|when\s+(?:do\s+you\s+)?(?:open|close)
```

**`is_working_days_question`** — add English pattern:
```
|(?:do\s+you\s+)?(?:work|open)\s+(?:on\s+)?(?:sunday|saturday|weekend|weekday)
|what\s+days?\s+(?:are\s+you\s+open|do\s+you\s+work)
|(?:open|working)\s+days?
```

**`_PAYMENT_RE`** — add: `|pay\s+(?:by|with)\s+card|credit\s+card|cash`
**`_LOCATION_RE`** — add: `|where\s+(?:are\s+you|is\s+it)|(?:your\s+)?(?:address|location|directions?|how\s+to\s+get)`
**`_BRING_RE`** — add: `|what\s+(?:to|should\s+(?:i|we))\s+bring|what\s+(?:do\s+(?:i|we)\s+)?need\s+to\s+bring`

## Acceptance Criteria
1. «Are there any discounts?» → `"I'll check with the team and get back to you."` (EN FAQ defer). ✅
2. «What time do you open?» → `"We're open from 08:00 to 21:00."` (EN hours line). ✅
3. «Do you work on weekends?» → `"We're open every day from 08:00 to 21:00."` (EN days line). ✅
4. «I'd like to talk to a real person» → `HUMAN_REQUEST_LINE_EN` (human-handoff copy, not booking). ✅
5. «Can I pay by card?» → English FAQ defer. ✅
6. «Where are you located?» → English FAQ defer. ✅
7. All Russian variants unchanged (regression test). ✅
8. Gates green; 100% coverage.

## Files
- `services/api/app/sales/sales_persona_answerer.py` (`_HUMAN_REQUEST_RE`, `_DISCOUNT_RE`, `_PAYMENT_RE`, `_LOCATION_RE`, `_BRING_RE`, `_SCHEDULE_RE` / `is_working_hours_question`, `is_working_days_question`)
- tests: `test_sales_persona_answerer_early_busy_check.py`
