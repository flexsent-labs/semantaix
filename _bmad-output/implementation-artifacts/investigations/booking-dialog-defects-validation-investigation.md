# Investigation: Validate booking-dialog QA defects D1–D6 against current main

## Hand-off Brief

1. **What happened.** A live-QA defect doc (`booking-dialog-defects-2026-06-01.md`) listed 6 booking-dialog defects (D1–D6); validating each against current `main` (HEAD `0ee1370`) shows **5 of 6 already have merged fixes** (stories 12.26–12.31, landed during the same window) and **D1 (P0) is the only fully-open defect** — *Confirmed*.
2. **Where the case stands.** Concluded. D1 OPEN; D2/D3/D4 PARTIALLY-RESOLVED (each merged story fixed its headline case but left a real sub-gap); D5/D6 RESOLVED. Token-persistence (D1 AC4) is already satisfied — the QA doc's "token dropped after restart" trigger is imprecise.
3. **What's needed next.** Fix **D1** first (P0, the only severe open gap; the fix pattern already exists on an adjacent path) → then decide whether the D2/D3/D4 sub-gaps warrant follow-up stories. Recommended path: `bmad-create-story` for D1.

## Case Info

| Field | Value |
| --- | --- |
| Ticket | N/A (validation of `booking-dialog-defects-2026-06-01.md`) |
| Date opened | 2026-06-01 |
| Status | Concluded |
| System | Semantaix sales persona (`services/api/app/sales/`, `services/api/app/calendar/`), main @ `0ee1370` |
| Evidence sources | Source code (read), `git log`/`git show` (12.24–12.31), merged story docs + tests, QA transcript log |

## Problem Statement

The QA doc presents D1–D6 as open defects "found after the 12.31 fix." But stories **12.26–12.30** were authored and merged to address D2–D6 during the same 31 May–1 June window, and **12.24/12.31** cover D6a. The doc largely predates those merges. Goal: independently confirm, per defect, whether it is resolved / partially resolved / still open in current code, and surface any sub-gaps the merged stories left behind.

## Confirmed Findings

### Finding D1 — P0 silent-accept on unverifiable calendar: **OPEN** (Confirmed)
**Evidence:**
- Completion gate acts only on UNAVAILABLE: `services/api/app/sales/sales_persona_answerer.py:1239`; `STATUS_NOT_CONNECTED`/`STATUS_ERROR`/`None` collapse to `free=False` → `_handoff_after_scoping` (`:1249-1250`), whose only text differentiator is `SLOT_FREE_HANDOFF_LINE if free else SCOPING_COMPLETE_HANDOFF_LINE` (`:1623`). A disconnected/erroring calendar therefore emits the same "Спасибо! Передам детали коллегам…" line as a genuine handoff — indistinguishable from a verified-free confirmation.
- Early gate falls through silently by design: `sales_persona_answerer.py:1559` (`if availability.status != STATUS_UNAVAILABLE: return None`), comment `:1555-1558` states NOT_CONNECTED/ERROR fall through "so we never escalate from the early gate on infra hiccups." Locked in by `tests/test_sales_persona_answerer_early_busy_check.py:7-8`.
- Status sources: `services/api/app/calendar/requested_time_check.py:102-108` (NOT_CONNECTED on missing token/freebusy/operator), `:125-136` (ERROR on reconnect_needed / token_not_found / provider_error).
- **Partial mitigation already exists on the sibling path:** the generic proposer escalates on provider error (`NO_PROPOSAL_PROVIDER_ERROR` → `PROPOSAL_FALLBACK_UNAVAILABLE` + `escalate=True`); the fix pattern is in-tree but was never wired into `_complete_booking` / `_maybe_intercept_busy_slot`.

### Finding D1-token — restart token persistence: **already satisfied** (Confirmed) — corrects the QA premise
**Evidence:** refresh token is Fernet-encrypted and DB-backed in `calendar_operator_tokens` (`services/api/app/calendar/token_repository.py:38-99`); the in-memory access-token cache is rebuilt from the DB on miss (`services/api/app/calendar/access_token_cache.py:142-144`). A plain restart does **not** drop connectivity. The silent-accept is triggered by a genuinely revoked/expired refresh token (persisted `reconnect_needed`, `access_token_cache.py:183-185`), missing wiring (no `operator_chat_id`), or a transient provider error — **not** by a restart per se. D1 AC4 is effectively a no-op; the real fix is AC1–AC3.

### Finding D2 — absolute dates skip busy check: **PARTIALLY-RESOLVED** (Confirmed)
**Evidence:** 12.26 (`7440516`) added `_extract_absolute_date` (`services/api/app/calendar/service_resolver.py:190-210`, regex `:155-158`, wired `:268-273`); natural-language `"<day> <month>"` (`1 июня в 14:00`) now parses tz-aware and intercepts busy slots (tests `tests/test_calendar_service_resolver.py:236-298`, `tests/test_sales_persona_answerer_early_busy_check.py:388-419`). **Gap (Confirmed by live probe):** non-word absolute formats still return `None` and skip the check — `01.06 в 14:00`, `1.06.2026`, ISO `2026-06-01`, `01/06`, and ordinal `первого июня` (the `_HH_MM` clock regex at `service_resolver.py:126` greedily eats `01.06`; `_ABS_DATE_RE` requires a Cyrillic month word + numeric day). Live impact depends on whether the scoping LLM emits numeric/ISO forms (it usually emits Russian words, which are covered) — *Deduced, lower likelihood*.

### Finding D3 — non-booking intent misrouted: **PARTIALLY-RESOLVED** (Confirmed)
**Evidence:** 12.27 (`4c6eee1`) added `is_cancellation` (`services/api/app/sales/cancel_intent.py:28,31-35`), gated before the funnel (`sales_persona_answerer.py:625-629`), with a labelled handoff `CANCELLATION_HANDOFF_LINE` + `hitl_reason='sales_cancellation_request'` (`:120-122,816-863`); tested (`tests/test_sales_cancel_intent.py`). **Gap (Confirmed):** D3 AC2 (reschedule/modify — "перенести", "поменять время") has no intent branch anywhere; "бронь" is a sales seed (`data/russian_sales_intent.txt:33`), so a reschedule turn enters the booking funnel and can emit the booking-acceptance line (`sales_persona_answerer.py:135`) — the same class of bug D3 flagged.

### Finding D4 — price/recommendation dropped: **PARTIALLY-RESOLVED** (Confirmed)
**Evidence:** 12.28 (`f4fe2f7`) added `_maybe_intercept_price_ask` (`sales_persona_answerer.py:779-783,1584-1610`); a first-turn price ask now routes to pricing before the date ask (AC1/AC3). **Gap (Confirmed):** D4 AC2 (recommendation/capacity — "что посоветуете", "нас 8 человек" → vehicle-count guidance) is unimplemented — `classify_turn` has no recommendation bucket (`turn_intent.py:157-180`), and there is **no buggy-capacity data or headcount→vehicle logic anywhere** in `services/api/app/`. A pure recommendation ask classifies as `other` → date funnel. Also `price_lookup._build_query` ignores `headcount`/`intent` (`price_lookup.py:99-110,131-160`) so quotes aren't group-size-aware (documented out-of-scope in 12.28).

### Finding D5 — wrong unavailable reason ("занято"): **RESOLVED** (Confirmed, Grade A)
**Evidence:** 12.29 (`765405b`) threads `RequestedAvailability.reason` (`requested_time_check.py:82,173`) into a reason→copy map `_UNAVAILABLE_LEAD_LINES` (`sales_persona_answerer.py:146-155`), branched at `:1437`, passed from both gates (`:1247`, `:1581`). Distinct lines for `outside_working_hours`/`wrong_service_day`/`date_exception`/`in_past`; `busy` keeps "занято"; nearest-free alternative still offered for all (`:1424-1438`). Tests `tests/test_sales_persona_answerer_off_hours_wording.py`. **Residual (low-impact, documented):** `REASON_OUTSIDE_LOOKAHEAD` (>60 days out) still falls back to `SLOT_BUSY_LINE` — deliberate per the story; AC5 only requires "handled sensibly."

### Finding D6 — duplicate sends + nondeterminism: **RESOLVED** (Confirmed)
**Evidence:**
- *D6b determinism:* the accept/reject verdict is a **pure function** (`compute_availability`, `services/api/app/calendar/availability.py:210,218` — no LLM), so it is structurally deterministic; the LLM only extracts fields and phrases the next question. 12.30 (`1f7acba`) additionally pins `_DETERMINISTIC_TEMPERATURE=0.0` on `complete_json` + `verify_grounding` (`openrouter_client.py:75,231,286`) — belt-and-suspenders for phrasing jitter. The "reject+accept in one breath" is a *coherent* "busy + no free alternative → handoff" line (`sales_persona_answerer.py:1432-1438`), one `AnswerResult.text`, not a contradiction — cosmetic at most.
- *D6a duplicate send:* three atomic dedup layers cover the customer line — webhook `update_id` claim (12.31, `services/bot_gateway/app/main.py:2405` → `webhook_dedup.py`), api `claim_inbound(trace_id)` (12.24, `services/api/app/main.py:2340` → `answer_trace.py:184-203`), finalized-replay gate (`main.py:2314`). Interim ack (12.13) is a *different* body, deferred/gated — not a duplicate of "Передам коллегам…".

## Deduced Conclusions

### Deduction 1: The QA doc largely predates the 12.26–12.31 merges
**Based on:** Findings D2–D6 each map 1:1 to a story merged 31 May–1 June (`git log`: 12.26→D2, 12.27→D3, 12.28→D4, 12.29→D5, 12.30→D6b, 12.24/12.31→D6a).
**Conclusion:** The doc is a valid pre-merge snapshot; re-validated, only **D1** has no corresponding fix, and three fixes (D2/D3/D4) closed their headline case but not their full acceptance criteria.

### Deduction 2: D1 is the single P0-worthy open item, and its fix is well-scoped
**Based on:** Finding D1 + Finding D1-token + the in-tree proposer mitigation.
**Conclusion:** D1 needs an "unverified" handoff branch (distinct copy + `calendar_verified=false` HITL metadata + reconnect alert) wired into `_complete_booking`/`_maybe_intercept_busy_slot`, mirroring the existing `proposal_provider_error` escalation. AC4 (token-survives-restart) is already met, so the story is smaller than the doc implies.

## Missing Evidence

| Gap | Impact | How to obtain |
| --- | --- | --- |
| What date string the scoping LLM actually stores in `intent.dates` for "1.06"/"первого июня"/weekday | Determines whether the D2 numeric/ISO gap is live or theoretical | Add debug logging of `intent.dates` over a QA pass, or inspect `answer_trace` request payloads |
| Live re-verification of D2/D5 with the calendar **reconnected** | QA Round 2 ran with the calendar unreachable (D1), so D2/D5 were never live-verified post-fix | Reconnect calendar (`/connect_calendar`), re-run probes #4–#6, #9–#11 from the QA log |

## Final Conclusion

**Confidence: High.** Validated all six against current code with `path:line` evidence and merged-commit cross-reference.

- **D1 — OPEN (P0).** Silent fall-through on `NOT_CONNECTED`/`ERROR` still confirms unverifiable slots as if free. Token persistence (AC4) already satisfied; fix = AC1–AC3.
- **D2 — PARTIALLY-RESOLVED (P1→P2).** NL `<day> <month>` fixed; numeric/ISO/ordinal absolute formats still skip the check.
- **D3 — PARTIALLY-RESOLVED (P2→P3).** Cancellation fixed; reschedule/modify (AC2) still mis-routes into the booking funnel.
- **D4 — PARTIALLY-RESOLVED (P3).** Price ask fixed; recommendation/capacity (AC2) unimplemented; quotes not group-size-aware.
- **D5 — RESOLVED.** Minor: `outside_lookahead` still reads "занято" (deliberate).
- **D6 — RESOLVED.** Verdict is deterministic by construction; duplicate-send covered by 3 layers. "занято … передам детали коллегам" combined line is coherent (cosmetic only).

### Fix direction
1. **D1 (P0)** — new "could-not-verify" handoff branch + `calendar_verified=false` HITL flag + operator reconnect alert (reuse `18754e9` dedup), wired into both gates. Mirror the `proposal_provider_error` escalation already in-tree. **Highest value.**
2. **D3-reschedule** — `reschedule_intent` sibling to `cancel_intent` (small, mirrors 12.27).
3. **D2-numeric-dates** — extend `_extract_absolute_date` for dotted/ISO/slash + ordinal words, and stop `_HH_MM` from eating `01.06` (medium).
4. **D4-recommendation** — headcount→vehicle-count guidance needs buggy-capacity data first (larger; product decision).

### Next steps menu
- **Recommended:** `bmad-create-story` for **D1** (P0, scoped, fix pattern exists).
- D3-reschedule / D2-numeric-dates: `bmad-create-story` as P3 follow-ups (or fold D3-reschedule into the existing D3 doc).
- D4-recommendation: `bmad-correct-course` / product scoping (capacity data dependency).
- D5/D6: close on the doc as resolved (cite the merged stories); optional cosmetic ticket for the combined "занято…handoff" line.
