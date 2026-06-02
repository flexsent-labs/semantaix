# Investigation: Round-11 booking-dialog defects (incl. 2 regressions of 12.47/12.49)

## Hand-off Brief

1. **What happened.** Round-11 live QA showed four defects; two (N2 wrong-service price, N3 EN→RU) are regressions of fixes I deployed hours earlier — and the deployed container *does* run my code (Confirmed), so the fixes are live but **inert against the real data/flow**.
2. **Where the case stands.** All four root causes are **Confirmed** by side-effect-free repro against the deployed code + live DBs: R11-2 (English never parses), R11-1 (time-only counter-offer misread as acceptance), N2 (zero services configured + RAG has only quadbike prices), N3 (two emit sites — pitching-followup + closing — never localized, reached because the EN/time-only message fell through the counter-offer detector).
3. **What's needed next.** Decide scope — three are code fixes (R11-2 EN parsing, R11-1 negotiation, N3 remaining emit sites); N2 is primarily a **data/config** problem (no buggy service, no buggy price) that needs an operator decision plus a code-hardening option. Recommend `bmad-create-story` per defect after the scope call.

## Case Info

| Field | Value |
|-------|-------|
| Ticket | round-11 live QA (booking-dialog-live-round11-2026-06-02.md) |
| Date opened | 2026-06-02 |
| Status | Concluded (root causes Confirmed) |
| System | Live `semantaix` compose stack; deploy worktree `sharp-feynman-a9a552` @ `ef54c30` (= origin/main, PR #129 merged); api container `semantaix-api-1` |
| Evidence sources | Deployed source (current worktree = origin/main), live SQLite DBs copied read-only from the container volume, side-effect-free repro via the venv python |

## Problem Statement

QA at 22:04–22:20 (after the 21:56 deploy) reported: R11-1 (negotiation books the bot's own time), R11-2 (English bookings skip the busy check), and that N2 (wrong-service price) and N3 (language) — both shipped in PR #129 — still fail live. Premise to verify: *why did two just-deployed, 100%-tested fixes not change live behavior?*

## Evidence Inventory

| Source | Status | Notes |
|--------|--------|-------|
| Deployed code (container) | Available | `localize` and `_best_matching_service` present in `semantaix-api-1` (`docker exec grep` = 1 / 3). Deploy is NOT the problem. |
| `extract_requested_start` behavior | Available | Ran deployed function on EN + RU inputs. |
| Live `semantaix_sales.db` (services) | Available | Copied read-only; `services` table = **0 rows**, `scoping_schemas` = 0. |
| Live `semantaix_rag.db` (price chunks) | Available | 220 chunks, 16 price-bearing — **all «за квадроцикл»/enduro/scooter; no buggy price**. |
| Sales answerer repro | Available | Reproduced N3 + R11-1 byte-exact via stub harness on the deployed class. |
| Live `sales_conversation_state` for Artur's chat | Not collected | Would confirm the exact stage at 22:14; deduced PITCHING/CLOSING from conv-C handoff. Low value — repro already byte-exact. |

## Timeline of Events

| Time | Event | Source | Confidence |
|------|-------|--------|------------|
| 21:56 | Deploy: stack FF to `ef54c30` (PR #129: 12.47, 12.49) | rebuild log | Confirmed |
| 22:04–22:13 | Conversations A/B/C run in one continuous Telegram thread; conv C ends with SLOT_FREE handoff → parks PITCHING | round-11 doc | Confirmed (doc) / Deduced (stage) |
| 22:14–22:20 | Conversation E (English) arrives while chat already in PITCHING/CLOSING | round-11 doc | Deduced |

## Confirmed Findings

### Finding 1 (R11-2): `extract_requested_start` parses no English — busy check can never fire on the EN path
**Evidence:** deployed `services/api/app/calendar/service_resolver.py#extract_requested_start` returns `None` for `"tomorrow at 2pm"`, `"tomorrow at 14:00"`, `"2pm"`, `"10am"`, `"June 4 at 10am"`; returns correct datetimes for `"завтра в 14:00"` → 2026-06-03 14:00 and `"03.06 в 16:00"` → 2026-06-03 16:00.
**Detail:** The parser is Russian-only (relative word «завтра», «в HH:MM», dotted/written-month). Every English booking yields no concrete `requested_start`, so the early/pitching busy check is skipped and the turn always hands off — exactly R11-2. This also *feeds* N3 and R11-1 on the EN path.

### Finding 2 (R11-1): a time-only follow-up is misclassified as acceptance of the prior alternative
**Evidence:** repro on the deployed class — state PITCHING, `last_proposal=08:00`, message «а давайте тогда в 12:00» → `sales_turn_kind=pitching_accept_confirmed`, reply «…передаю детали коллеге на подтверждение — 3 июня на **08:00**.» Correction «Нет, мне нужно именно в 12:00, а не в 08:00» → `pitching_followup`, generic handoff. Byte-exact to the live transcript.
**Detail:** In `_handle_pitching` (sales_persona_answerer.py:1291–1309): (1) `_merge_dates_from_customer_message` only adopts a counter-offer when a concrete **date+time** parses — a time-only «в 12:00» (date implied by context) parses to `None` (both `parse_russian_date_span` and `extract_requested_start` need a date), so it is not treated as a counter-offer. (2) The next branch `is_acceptance(question)` (acceptance.py) matches the leading «давайте» and confirms the stored 08:00. So a *counter-proposal* is read as *acceptance of the bot's slot*. The correction then matches neither branch → `_handoff_after_pitching_followup`, no 12:00 verdict.

### Finding 3 (N2): zero services configured AND the RAG catalog has no buggy price — the 12.49 guard is inert
**Evidence:** live `semantaix_sales.db` `services` table = 0 rows; `scoping_schemas` = 0. Live `semantaix_rag.db`: 16 price-bearing chunks, all «СТОИМОСТЬ: … ₽ ЗА КВАДРОЦИКЛ» / enduro / scooter (ids 91/101/109/118/127/145/181/194 …). No chunk prices a buggy. Catalog digest lists quadbike/enduro/scooter tours.
**Detail:** `_handle_pricing` passes `service_names = services_repo.list_for_project(...)` = `[]`. In `price_lookup.py#lookup`, `_best_matching_service(question, (), normalizer)` iterates an empty list → `asked = None`; the per-service filter `if asked is not None and …` is therefore **skipped entirely**, so the lookup returns the **first price-bearing chunk** — a quadbike price (13 000 / 30 000 ₽). 12.49 is correct *given a service catalog*, but the live project has none, and even a correct catalog has no buggy price to return. **This is a data/config defect first, a code-hardening opportunity second.** (The "capacity recommendation dropped" sub-item, D4, is a separate missing feature in the pricing path.)

### Finding 4 (N3): two customer-facing emit sites were never localized; reached via the pitching/closing fall-through
**Evidence:** `grep` of the deployed source — `SCOPING_COMPLETE_HANDOFF_LINE` is emitted **bare** at `sales_persona_answerer.py:1410` (`_handoff_after_pitching_followup`); `CLOSING_HANDOFF_LINE` bare at `:3046` and `:3082`. Repro: PITCHING + an English non-counter-offer → `pitching_followup` → «Спасибо! Передам детали коллегам на подтверждение — вернутся с ответом.» (RU) with `ctx.language` correctly `"en"`. `try_answer` *does* set `ctx = replace(ctx, language=detect_language(question))` (Confirmed in deployed source), and `detect_language("Hi! I'd like to book a buggy…")` = `"en"`.
**Detail:** 12.47 localized the scoping/busy/free/ask sites but **missed** `_handoff_after_pitching_followup` and the closing handler (the latter I explicitly scoped out). Because the QA was one continuous thread, Artur was in PITCHING/CLOSING by conversation E, and the EN/time-only messages fell through the counter-offer detector (Finding 1) straight onto these un-localized lines. The 12.47 tests only asserted the sites I changed — they never asserted that *no* customer-facing constant is emitted bare, so the gap was invisible to the gate.

## Deduced Conclusions

### Deduction 1: "100% tests passed but live failed" = tests encoded fabricated data/paths
**Based on:** Findings 3 + 4.
**Reasoning:** 12.49 tests passed `service_names=["Аренда багги","Квадроцикл"]` (live has none); 12.47 tests exercised only the booking-journey sites (live hit the pitching/closing fall-through). Both fixes are individually correct against their test fixtures; neither fixture matched live reality (no services; mid-funnel English thread).
**Conclusion:** Coverage % measured the code I wrote, not the live surface. Re-fixes need tests anchored to live conditions: empty `service_names`; question-vs-chunk subject mismatch; a "no bare customer constant" guard; mid-funnel EN turns.

### Deduction 2: R11-1, R11-2, and the N3 fall-through share one weak seam — the counter-offer detector
**Based on:** Findings 1, 2, 4.
**Reasoning:** Requiring a full date+time and Russian-only parsing makes the detector miss (a) English times and (b) time-only follow-ups with an implied date; misses fall to acceptance (R11-1) or to the un-localized handoff (N3), and never reach the busy check (R11-2).
**Conclusion:** Strengthening counter-offer detection (date carryover from context + English tokens) addresses the common cause of three symptoms.

## Fix Direction (investigation stops at diagnosis; for the scope call)

- **R11-2 (P1, code):** add English token support to `extract_requested_start` ("tomorrow", "today", am/pm clock, "June 4", weekday names) → same tz-aware datetime + same busy check. Highest leverage (also unblocks N3/R11-1 on the EN path).
- **R11-1 (P2, code):** in `_handle_pitching`, (a) treat a time-only follow-up as a counter-offer by inheriting the date from context/`last_proposal` and re-running the slot check; (b) gate `is_acceptance` so it does not fire when the message names a concrete *different* time; (c) order counter-offer detection before acceptance.
- **N3 (P3, code):** localize the remaining customer-facing emit sites (`_handoff_after_pitching_followup` :1410; closing :3046/:3082) and add a guard/test asserting no customer-facing constant is returned bare. Mechanism is the existing `localize(..)`/`ctx.language`.
- **N2 (data/config first):** the live project has **no services and no buggy pricing**; the bot is a "buggy rental" persona over a quadbike tour catalog. Needs an operator decision — (i) configure the buggy service(s) + buggy price in RAG, and/or (ii) code-harden the price lookup to compare the **question's subject noun vs the chunk's subject noun directly** (not only via the empty service catalog) so a «багги» ask never quotes a «квадроцикл» price — escalate (`PriceMissing`) instead. Plus the dropped capacity-recommendation (D4) is a separate feature gap.

## Reproduction Plan

All side-effect-free against the deployed code (no live `/conversations/inbound`, which would DM the operator):
- **R11-2:** `extract_requested_start(text="tomorrow at 2pm", now=<2026-06-02 22:15 MSK>, project_tz=MSK)` → `None`.
- **R11-1:** stub `SalesPersonaAnswerer` in PITCHING with `last_proposal={alternative_iso:"2026-06-03T08:00:00+03:00"}`; send «а давайте тогда в 12:00» → `pitching_accept_confirmed` @ 08:00.
- **N2:** `PriceLookup.lookup(service_names=(), question="сколько стоит покататься на багги")` against live `semantaix_rag.db` → a «квадроцикл» `PriceFound`.
- **N3:** same PITCHING stub, any English non-counter-offer → RU `SCOPING_COMPLETE_HANDOFF_LINE` with `ctx.language="en"`.
