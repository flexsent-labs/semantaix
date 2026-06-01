# Story 12.35: Reply in the customer's language (D8, P3)

Status: review

## Story

As an **English-speaking customer**,
I want the bot to **answer in English**,
so that **I can understand the reply.**

**Problem (observed live, багги, 1 June 2026 10:57):**

```
Artur:  Hi! Can I book a buggy for 4 people tomorrow at 10am? How much does it cost?
Анна:   Сколько багги вам нужно?     (Russian reply to an English message)
```

Field extraction from English works; only the reply language is wrong.

**Root cause (CONFIRMED — see investigation Finding D8):** every sales LLM prompt hardcodes the reply language as Russian (`"<твоё короткое сообщение клиенту на русском>"` in `sales_greeting.txt`/`sales_scoping.txt`/`sales_proposal.txt`/`sales_pricing_hit.txt`/`sales_concept_rag.txt`, and `sales_catalog.txt` is fully Russian). `AnswerContext.language` exists but is a static platform default ("ru"), never derived from the message, and the sales answerer never reads it.

## Scope (this story)

The reported symptom is the **LLM-generated funnel question** (the scoping question came back Russian to an English customer). This story makes the **greeting + scoping** prompts mirror the customer's language (Russian or English) — covering the entire field-collection back-and-forth, which is the bulk of the customer conversation and exactly the reported case. **No Python change** (prompt-text only), so zero funnel-logic risk.

**Deferred (documented follow-ups):**
- **`proposal` / `pricing_hit` / `concept_rag` prompts** carry verbatim-token / grounding verifiers that expect Russian output (e.g. `_proposal_text_matches` asserts the Russian date string appears verbatim; mirroring to English would fail the verifier → spurious escalation). Localizing those needs verifier-aware date/price formatting — a larger, riskier change kept out of this safe increment.
- **~20 deterministic customer-facing constants** (handoff/busy/decline lines) stay Russian. Coherent localization needs the conversation language persisted per-chat (a sales-state schema change) so a later letter-less reply doesn't flip the language.

So an EN customer now gets EN greeting + scoping questions; later handoff/proposal lines remain RU until the follow-ups land.

## Acceptance Criteria

1. The greeting + scoping prompts instruct the model to reply **in the same language the customer used** (default Russian when ambiguous), replacing the hardcoded "на русском".
2. Field extraction from non-Russian input is unchanged (it already works — extraction is separate from the reply-language instruction).
3. Russian conversations are unchanged (mirroring a Russian message → Russian).
4. Gates green; 100% coverage (no new Python branches — prompt text only).

## Tasks / Subtasks

- [x] `sales_greeting.txt`, `sales_scoping.txt`: replace the hardcoded Russian-output placeholder with "на том же языке, на котором написал клиент (русский или английский); по умолчанию русский".
- [x] Tests: the greeting + scoping system prompts (via the answerer's builders) carry the language-mirroring instruction and no longer pin Russian-only output.
- [ ] (Follow-up) verifier-aware mirroring for proposal/pricing/concept + RU/EN localization of the deterministic constants.

## Dev Notes

- **Why prompt-only / LLM-mirrored:** the LLM writes these lines and already extracts EN fields, so a mirroring instruction is the minimal, reliable fix with zero funnel-logic risk. Detection-in-code + a RU/EN constant map would be larger and needs per-chat language persistence to stay coherent across letter-less turns — deferred.
- **Files:** the six `services/api/app/sales/system_prompts/sales_*.txt` prompts. No `.py` changes.
- **Note:** the grounded-RAG answer path has the same hardcoded-RU exposure in its own prompts but is out of scope for this sales-persona defect.

## References

- Investigation: round-3/4 validation, Finding D8 (`ctx.language` exists but is a static default; sales prompts hardcode RU).
- [Source: services/api/app/sales/system_prompts/sales_greeting.txt], [sales_scoping.txt], [sales_proposal.txt], [sales_pricing_hit.txt], [sales_concept_rag.txt], [sales_catalog.txt].
- Precedent: `services/api/app/answerers/scheduling_context.py` already branches on `ctx.language` (`in_russian`).

## Dev Agent Record

### Agent Model Used

claude-opus-4-8 (Claude Code)

### Completion Notes List

- **Shipped (AC 1–4) for the funnel prompts.** `sales_greeting.txt` + `sales_scoping.txt` now instruct the LLM to reply on the customer's language ("на том же языке, на котором написал клиент … по умолчанию русский") instead of hardcoded "на русском". Prompt-text only — no Python change, no funnel-logic risk, no new coverage branches.
- **Scoped deliberately:** proposal/pricing/concept prompts kept RU (their verbatim/grounding verifiers expect Russian — mirroring would fail them); deterministic constant lines kept RU (need per-chat sticky language = a state-schema change). Both are documented follow-ups.
- **TDD:** tests written first, watched fail (prompts lacked the mirror directive), then green — greeting + scoping builders carry the directive and no longer pin Russian-only output.
- `ruff` clean; full suite green at 100% coverage (no Python change).

### File List

- `services/api/app/sales/system_prompts/sales_greeting.txt` (modified)
- `services/api/app/sales/system_prompts/sales_scoping.txt` (modified)
- `tests/test_sales_persona_answerer_language.py` (new)
- `_bmad-output/implementation-artifacts/12-35-reply-in-customer-language.md` (new)
- `_bmad-output/implementation-artifacts/sprint-status.yaml` (modified)
