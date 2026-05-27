# Semantaix Epics and Stories (Feature-Sequential)

This directory contains the BMAD feature-based sequential epic layout.

## Hard Rule
- Only one feature epic can be in implementation at a time.
- No feature from later epics may be implemented early.
- Next epic starts only after:
  - story tests pass
  - feature regression check passes
  - demo/acceptance signoff is completed

## Epic Order
1. `epic-01-telegram-llm-suggestions.md`
2. `epic-02-incident-alert-foundation.md`
3. `epic-03-guardrails-validity.md`
4. `epic-04-hitl-escalation.md`
5. `epic-05-rag-foundation.md`
6. `epic-06-knowledge-moderation.md`
7. `epic-07-backup-restore-hardening.md`
8. `epic-08-tenant-knowledge-ops-and-answer-traces.md`
9. `epic-09-operator-kb-growth.md`
10. `epic-10-multi-operator-projects.md`
10.5. `epic-10-5-operator-project-model-refinement.md` *(backlog — refinement to support Epic 14)*
11. `epic-11-calendar-availability-scheduling.md`
12. `epic-12-sales-conversation-persona.md` *(planning merged via PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) + cap PR [#83](https://github.com/flexsent-labs/semantaix/pull/83); stories backlog)*
13. `epic-13-unified-project-services-catalog.md` *(shipped via PR [#80](https://github.com/flexsent-labs/semantaix/pull/80))*
14. `epic-14-usage-cost-monitoring.md` *(planning — Step 2 of `bmad-create-epics-and-stories` complete; queued post-Epic 10.5 for stories 14.07 + 14.08; other stories can proceed in parallel)*

## Recent Implementation Notes
- **Epic 04 (HITL escalation):** runtime HITL recipient/chat routing can be updated by Telegram command `/hitl_config @username <chat_id>`.
- **Access control:** only `HITL_CONFIG_ADMIN_USERNAME` (currently `@ajdevy`) is authorized to apply runtime HITL configuration changes.
- **Epic 09 (Operator KB growth):** the trusted HITL operator can grow the knowledge base from Telegram via slash command `/kb_add [confidential]` or Russian free-text intent (e.g. "добавь в базу", "сохрани в kb"). Supports PDF/DOCX/PPTX/TXT, image OCR (tesseract), and audio/video transcription (faster-whisper) — all local, zero external API spend. Uploads auto-publish (no second-human review); `confidential` uploads ground answers but redact `source_id` and `chunk_text` in answer-trace metadata.
- **Access control:** only the effective operator (runtime `hitl_primary_operator_username` or env default) can trigger `/kb_add`; non-operator messages are ignored with reason `unauthorized_kb`.
- **Epic 11 (Calendar availability & scheduling):** opt-in per project (default-off). The project's designated calendar operator connects their own Google Calendar via `/connect_calendar` (read-only OAuth); the bot answers customer availability questions by intersecting `freeBusy` with per-service rules in the project timezone. Read-only first (no booking). Uncertainty escalates to the calendar operator. See `epic-11-calendar-availability-scheduling.md` + `stories/epic-11/`.
- **Epic 12 (Sales conversation persona) — planned:** always-on for every project (no enable command, no services-count gate; the sales-intent regex is the only entry gate). New `SalesPersonaAnswerer` runs a multi-turn consultative dialog: gathers intent, autonomously dispatches videos/photos/PDFs from a `client_materials` library, looks up prices in the KB and escalates-then-learns when a price is unknown (operator HITL reply auto-extracts into the KB via the Epic-06 loop), proposes a calendar slot via Epic 11, and nudges +1d (skip-if-stale). Services populate the `services` table via three input paths: slash commands, natural-language operator dialog, and automatic LLM extraction from `/kb_add` uploads. KB-uploaded files are also analyzed for client-sendability and registered as materials. Discounts deferred. See `epic-12-sales-conversation-persona.md` + `stories/epic-12/`.
- **Epic 13 (Unified Project Services Catalog):** one canonical structured `project_services` table per project (renames Epic 11's `calendar_service_rules` + adds catalog columns) powers BOTH the catalog answer and the calendar; a row is catalog-eligible always and calendar-eligible iff `duration_minutes IS NOT NULL`. Two operator entry paths converge on one repo under a per-`(project_id, lower(name))` single-flight lock: `/service add|edit|remove|list` slash command and a Russian natural-language propose/confirm/cancel dialog (mirrors `admin_nl_ops`). `/service remove` is operator-only (preserves Epic 11's destructive-op rule); add/edit shared with admin only when that admin is also a registered project operator. The catalog answer merges structured rows with the existing `_catalog_digest` output via lemma-based dedup (`RussianNormalizer.lemmas`; structured wins on conflict), rendered as natural Russian prose at the repository boundary with NO field labels. Every `services_nl_op_*` event logs the full operator-typed payload (service content is non-secret published data). Migration is idempotent + has a fresh-deploy path. **Note on numbering:** originally planned as Epic 12 (PR #80); renumbered to Epic 13 on merge with main, which had a parallel Epic 12 (Sales Conversation Persona). See `epic-13-unified-project-services-catalog.md` + `stories/epic-13/`.

## Carry-forward Constraint
From Epic 03 onward, every epic must integrate with the incident/alerts solution from Epic 02.

## Automated E2E

Story-aligned pytest node ids (including Epic 07 backup/restore and Epic 08 traces/NL-ops/correction) live in **`_bmad-output/implementation-artifacts/e2e-coverage.md`**. CI runs **`pytest`** with coverage plus **`pytest -m e2e`**.
