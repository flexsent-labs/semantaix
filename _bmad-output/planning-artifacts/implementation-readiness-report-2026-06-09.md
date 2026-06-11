---
date: 2026-06-09
project: Semantaix
stepsCompleted: [1, 2, 3, 4, 5, 6]
documentsInventoried:
  prd: PRD.md
  architecture: architecture.md
  epics: epics.md
  epicFolder: epics/
  storiesFolder: epics/stories/
  epicsCovered: [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
  storyFiles: 76
scope: Epic 15 (Telegram User Account Gateway) — new IR check post-Epic-15 story creation
---

# Implementation Readiness Assessment Report

**Date:** 2026-06-09
**Project:** Semantaix

---

## PRD Analysis

> **Scope note:** This report is a focused IR check on **Epic 15 (Telegram User Account Gateway)**. Epics 1–14 were covered by prior readiness reports (2026-05-22, 2026-05-24). PRD.md (FR-1–FR-36, NFR-1–NFR-11) covers Epics 1–14 only. Epic 15 requirements are defined in `epics.md` under the Epic 15 section — this is an acknowledged gap between PRD.md and the epics document, noted below.

### PRD Completeness Assessment

`PRD.md` is complete and coherent for Epics 1–14. **Epic 15 requirements do not appear in `PRD.md`.** They are defined in `epics.md` (§ "Epic 15: Telegram User Account Gateway") with their own FR-15-xx / NFR-15-xx numbering convention. This is a known planning artifact: the project moved Epic 15 requirements directly into the epics document without backfilling the PRD. This creates a traceability gap but is not a blocking implementation issue given the detailed story files that exist.

**PRD-level FRs (Epics 1–14):** FR-1 through FR-36 (36 total)
**PRD-level NFRs:** NFR-1 through NFR-11 (11 total)

### Functional Requirements — Epic 15 (from `epics.md`)

| # | Requirement | Story |
|---|-------------|-------|
| FR-15-01 | New `user_gateway` FastAPI service port 8005; Telethon MTProto; listens only to private DMs (`e.is_private == True`) | 15.01 |
| FR-15-02 | `POST /auth/qr_start` — calls `client.qr_login()`, renders QR PNG via `qrcode[pil]`, returns base64 + 30s expiry, starts background scan-wait task | 15.02 |
| FR-15-03 | `GET /auth/status` — returns `{"authenticated": bool, "phase": str}` for `bot_gateway` to poll | 15.02 |
| FR-15-04 | `POST /auth/verify_2fa` — accepts `{"password": "..."}` for 2FA accounts; password never logged or persisted | 15.02 |
| FR-15-05 | On QR timeout: call `qr_login.recreate()`; `bot_gateway` sends fresh QR to operator | 15.02 |
| FR-15-06 | On successful auth: session saved to `.data/user_gateway.session` on shared `app_data` volume | 15.02 |
| FR-15-07 | `bot_gateway` `/user_login` command orchestrates full auth flow (QR as document, status polling, timeout resend, 2FA relay) | 15.02 |
| FR-15-08 | Telethon `NewMessage` handler forwards customer messages to `api POST /conversations/inbound` via `ApiClient` + `internal_service_token` | 15.03 |
| FR-15-09 | Message router filters operator account messages to prevent double-routing with `bot_gateway` | 15.03 |
| FR-15-10 | `asyncio.Queue(maxsize=100)` decouples MTProto event receipt from HTTP forwarding | 15.03 |
| FR-15-11 | Reconnect watchdog: `while True` around `client.run_until_disconnected()` with exponential backoff (1s → 60s max) | 15.03 |
| FR-15-12 | Added to `docker-compose.yml`: `app_data` volume, health check port 8005, `restart: unless-stopped`, `depends_on: api: service_healthy` | 15.01 |
| FR-15-13 | `/health/live` via `create_service_app("user_gateway", lifespan=lifespan)` | 15.01 |
| FR-15-14 | Silent drop: `sender.scam == True` or `sender.fake == True` | 15.04 |
| FR-15-15 | Silent drop: `sender.bot == True` | 15.04 |
| FR-15-16 | Silent drop: forwarded messages with anonymous origin (`fwd_from.from_name` set) | 15.04 |
| FR-15-17 | Per-sender rate limiting via `InboundRateLimitRepository` (moved to `platform_common/`); shared `inbound_rate_limit_db_path` spans both channels | 15.04 |
| FR-15-18 | Silent drop: messages with >3 URLs | 15.04 |
| FR-15-19 | Apply `settings.inbound_max_message_chars` truncation before forwarding | 15.04 |
| FR-15-20 | Silent drop: configurable spam keywords in flat text file (`data/spam_keywords.txt`) | 15.04 |

**Total Epic 15 FRs: 20**

### Non-Functional Requirements — Epic 15 (from `epics.md`)

| # | Requirement | Story |
|---|-------------|-------|
| NFR-15-01 | 100% test coverage on all `services/user_gateway/` code (`.coveragerc` enforces) | All |
| NFR-15-02 | `client.flood_sleep_threshold = 60` set on Telethon client | 15.02 |
| NFR-15-03 | 2FA password never persisted to any store; never logged | 15.02 |
| NFR-15-04 | Session file path never logged; follow `_redact_token` pattern from `bot_gateway` | 15.02 |
| NFR-15-05 | Receive-only — `user_gateway` never sends any automated reply; all spam/rate-limit drops are silent | 15.03, 15.04 |
| NFR-15-06 | One new `.env` key only: `TG_USER_SESSION_PATH=.data/user_gateway.session` | 15.01 |
| NFR-15-07 | All new code: `from __future__ import annotations`, keyword-only public methods, `Protocol` interfaces, constructor-injected deps | All |
| NFR-15-08 | Spam filter drop decisions logged at DEBUG with `sender_id` and `reason_code`; message content never logged | 15.04 |
| NFR-15-09 | Silent drop on rate limit — no reply sent (diverges from `bot_gateway` which sends the Russian reply) | 15.04 |

**Total Epic 15 NFRs: 9**

### Additional Requirements / Constraints (Epic 15)

- **2FA in-memory state durability**: `AuthSessionRepository` (SQLite singleton row) tracks `phase` across restarts. On restart, stale `qr_pending`/`2fa_pending` phases cleared to `idle`; operator re-runs `/user_login` (graceful recovery, not resumption — Telethon `QRLogin` is non-serializable).
- **QR as document**: `send_document` not `send_photo` to prevent Telegram compression.
- **Story 15.05 deferred**: Cross-service dedup (bot_gateway + user_gateway both in same groups) conditional on production evidence; no story file created.
- **`InboundRateLimitRepository` refactor**: moved from `services/bot_gateway/` to `platform_common/` in Story 15.04 — a cross-service refactor with `bot_gateway` import update required.

---

## Epic Coverage Validation

### Coverage Matrix — PRD FRs 1–36 vs Epics 1–14

All 36 PRD FRs are fully covered. Summary (see prior readiness reports for detail):

| FR | Requirement | Epic | Status |
|----|-------------|------|--------|
| FR-1 | Telegram Conversation Flow | Epic 01 | ✅ |
| FR-2 | RAG Retrieval and Answering | Epic 05 + 01 | ✅ |
| FR-3 | HITL Escalation | Epic 04 | ✅ |
| FR-4 | Configurable HITL Recipient | Epic 04 | ✅ |
| FR-5 | Transcript Storage + Candidate Extraction | Epic 01 + 06 | ✅ |
| FR-6 | Knowledge Moderation | Epic 06 | ✅ |
| FR-7 | Alerts/Incidents UI | Epic 02 | ✅ |
| FR-8 | Critical Telegram Notifications | Epic 02 | ✅ |
| FR-9 | Health Endpoints | Epic 01 / cross-cutting | ✅ |
| FR-10 | Structured Logging + Trace | Epic 01 / cross-cutting | ✅ |
| FR-11 | Provider Resilience | Epic 02 + 03 | ✅ |
| FR-12 | Docker-First Runtime | Epic 01 / cross-cutting | ✅ |
| FR-13 | Guardrail Decision Engine | Epic 03 | ✅ |
| FR-14 | Backup/Restore | Epic 07 | ✅ |
| FR-15 | Tenant Answer Transparency | Epic 08 | ✅ |
| FR-16 | NL Tenant Knowledge Ops | Epic 08 | ✅ |
| FR-17 | Trace-Originated Correction Loop | Epic 08 | ✅ |
| FR-18 | Calendar OAuth Connect | Epic 11 | ✅ |
| FR-19 | Calendar Availability Answering | Epic 11 | ✅ |
| FR-20 | Per-Service Scheduling Rules | Epic 11 → 13 | ✅ |
| FR-21 | Per-Project Opt-In Gating | Epic 11 | ✅ |
| FR-22 | Service Resolution from Russian Text | Epic 11 / 13 | ✅ |
| FR-23 | Canonical `project_services` Table | Epic 13 | ✅ |
| FR-24 | Operator Service Editing Surface | Epic 13 | ✅ |
| FR-25 | Catalog Answer Reads Structured First | Epic 13 | ✅ |
| FR-26 | Three-Tracker Usage Architecture | Epic 14 → Story 14.01 | ✅ |
| FR-27 | Polymorphic Ingestion Seam | Epic 14 → Story 14.02 | ✅ |
| FR-28 | LLM Usage Capture | Epic 14 → Story 14.02 | ✅ |
| FR-29 | Message-Volume Capture | Epic 14 → Story 14.03 | ✅ |
| FR-30 | HITL Event Capture | Epic 14 → Story 14.04 | ✅ |
| FR-31 | Daily Roll-up + 30d Retention | Epic 14 → Story 14.05 | ✅ |
| FR-32 | Usage Dashboard (Web UI) | Epic 14 → Story 14.06 | ✅ |
| FR-33 | Usage API + RBAC | Epic 14 → Story 14.07 | ✅ |
| FR-34 | Role-Aware `/usage` Bot Command | Epic 14 → Story 14.08 | ✅ |
| FR-35 | Cost-Spike Alerting | Epic 14 → Story 14.09 | ✅ |
| FR-36 | Incident Grouping (Usage) | Epic 14 → Story 14.09 | ✅ |

### Coverage Matrix — Epic 15 FRs vs Stories 15.01–15.04

| FR | Requirement | Story | Status |
|----|-------------|-------|--------|
| FR-15-01 | user_gateway service on port 8005, Telethon, private DMs only | 15.01 | ✅ |
| FR-15-02 | `POST /auth/qr_start` | 15.02 | ✅ |
| FR-15-03 | `GET /auth/status` | 15.02 | ✅ |
| FR-15-04 | `POST /auth/verify_2fa` | 15.02 | ✅ |
| FR-15-05 | QR refresh on timeout | 15.02 | ✅ |
| FR-15-06 | Session persistence to shared volume | 15.02 | ✅ |
| FR-15-07 | `bot_gateway` `/user_login` command | 15.02 | ✅ |
| FR-15-08 | NewMessage → `api /conversations/inbound` | 15.03 | ✅ |
| FR-15-09 | Operator account message filter | 15.03 | ✅ |
| FR-15-10 | asyncio.Queue(maxsize=100) decoupling | 15.03 | ✅ |
| FR-15-11 | Reconnect watchdog + exponential backoff | 15.03 | ✅ |
| FR-15-12 | docker-compose.yml integration | 15.01 | ✅ |
| FR-15-13 | `/health/live` via `create_service_app` | 15.01 | ✅ |
| FR-15-14 | Scam/fake sender filter | 15.04 | ✅ |
| FR-15-15 | Bot sender filter | 15.04 | ✅ |
| FR-15-16 | Anonymous-forward filter | 15.04 | ✅ |
| FR-15-17 | Rate limiting (shared `InboundRateLimitRepository`) | 15.04 | ✅ |
| FR-15-18 | URL flood filter (>3 URLs) | 15.04 | ✅ |
| FR-15-19 | Length truncation | 15.04 | ✅ |
| FR-15-20 | Spam keyword filter | 15.04 | ✅ |

### Missing Requirements

**⚠️ GAP-1 (Low Severity): Epic 15 FRs/NFRs not in `PRD.md`**
- Epic 15's FR-15-01 through FR-15-20 and NFR-15-01 through NFR-15-09 exist only in `epics.md`.
- `PRD.md` ends at FR-36 / NFR-11 and has no section for the Telegram User Account Gateway feature.
- Same pattern as FR-26–FR-36 (Epic 14 addendum noted in epics.md: "PRD must be amended").
- **Impact**: Traceability gap — a reader inspecting only PRD.md would not know Epic 15 exists.
- **Recommendation**: Amend PRD.md to add a Feature Group section for Epic 15 (FR-15-01 through FR-15-20 and NFR-15-01 through NFR-15-09), matching the pattern of the Epic 11 / 13 / 14 sections. Non-blocking for implementation.

**⚠️ GAP-2 (Low Severity): FR Coverage Map in `epics.md` not updated for Epic 15**
- The `### FR Coverage Map` table (lines 158–208 in epics.md) does not include rows for FR-15-xx.
- **Recommendation**: Append Epic 15 rows to the coverage map table after implementation begins.

**ℹ️ DEFERRED: Story 15.05 (cross-service dedup)**
- Conditional — depends on production evidence of bot_gateway + user_gateway sharing Telegram group membership.
- No story file created; `epics.md` documents the deferral rationale. Appropriate.

### Coverage Statistics

| Scope | FRs | Covered | Coverage |
|-------|-----|---------|----------|
| PRD FRs 1–36 (Epics 1–14) | 36 | 36 | **100%** |
| PRD NFRs 1–11 | 11 | 11 | **100%** |
| Epic 15 FRs (from epics.md) | 20 | 20 | **100%** |
| Epic 15 NFRs (from epics.md) | 9 | 9 | **100%** |

---

## UX Alignment Assessment

### UX Document Status

**Not Found** — no dedicated UX design document exists in the planning artifacts. This was noted in Document Discovery (Step 1).

### Assessment

No UX document is needed for Epic 15. `user_gateway` is a purely backend MTProto service:
- No customer-facing UI (all customer interaction is via existing Telegram bot)
- No admin web UI changes (auth is orchestrated via bot DMs and the existing `/user_login` command)
- The "UI" for auth is the QR code sent as a Telegram document and the bot dialog — documented inline in FR-15-02, FR-15-07, and Story 15.02

**Broader project UX note**: the project's Web UI (admin shell at `/admin`) is covered by existing design decisions embedded in epic-level FRs (e.g. FR-7 Alerts tab, FR-32 Usage Dashboard). No separate UX spec has ever been created — UX is expressed as acceptance criteria within story files. This is a known, accepted pattern for this project.

### Warnings

None for Epic 15. No UI is implied or missing.

---

## Epic Quality Review

### Scope

Focused on Epic 15 stories (15.01–15.04) against create-epics-and-stories best practices. Epics 1–14 were validated by prior readiness reports.

### Epic 15 — User Value Focus ✅

**Epic Goal**: "Enable customer DMs received on the Telegram user account to reach the existing answer pipeline." This is user-centric — customers get a second inbound channel; operators get message routing without code changes. The capability stands alone (once shipped, it functions independently of future epics).

**Delivery dependency**: Epic 15 depends on shipped epics only:
- Epic 01 — `api /conversations/inbound` ✅ shipped
- Epic 04 — HITL ticket flow ✅ shipped
- `platform_common/` app_factory, settings, ApiClient patterns ✅ shipped
- `bot_gateway/app/rate_limit_repository.py` ✅ exists (moved to `platform_common/` in 15.04)

No dependency on any unshipped epic.

### Story-Level Validation

#### Story 15.01 — Service Skeleton + Docker Integration

| Check | Result |
|-------|--------|
| User value | ⚠️ Technical/infrastructure ("platform engineer wants deployable service") — foundational story, same pattern as Story 10.01. Acceptable. |
| Independent | ✅ No dependencies on other 15.xx stories |
| No forward deps | ✅ Clean |
| Story sizing | ✅ Appropriate — Dockerfile, docker-compose block, Settings fields, .env.example, CLAUDE.md |
| ACs testable | ✅ Health endpoints + Settings defaults — verified by unit tests |

#### Story 15.02 — QR Authentication Flow

| Check | Result |
|-------|--------|
| User value | ✅ "Operator authenticates user account via /user_login → QR → 2FA" — clear value |
| Independent | ✅ Backward dep on 15.01 only |
| No forward deps | ✅ Clean |
| Story sizing | ⚠️ Large but cohesive — spans user_gateway (auth endpoints + `AuthSessionRepository` + `_AuthState`) AND bot_gateway (`/user_login` command + `UserGatewayClient`). Cross-service scope is acceptable because the two are tightly coupled (the bot command and the auth endpoints are one user flow). |
| ACs testable | ✅ T1–T6 unit tests + integration tests for happy path and 2FA path — comprehensive |
| 2FA state durability | ✅ `AuthSessionRepository` SQLite singleton handles the restart-recovery scenario explicitly; graceful recovery documented |

#### Story 15.03 — Message Routing + Resilience

| Check | Result |
|-------|--------|
| User value | ✅ "Customer DMs forwarded to answer pipeline with queue buffering and reconnection" |
| Independent | ✅ Backward deps on 15.01, 15.02 only |
| No forward deps | ✅ Spam filter is a stub returning `False` — explicit deferral, not a dependency on 15.04 |
| Story sizing | ✅ Appropriate — `MessageRouter`, `asyncio.Queue`, watchdog, `ApiClientUserGateway` |
| Stub window | ℹ️ Between 15.03 and 15.04 completion, the service has no spam filtering. Acceptable since 15.04 is the immediate next story. |
| ACs testable | ✅ Comprehensive unit tests for all router/watchdog/client edge cases |

#### Story 15.04 — Spam Filters

| Check | Result |
|-------|--------|
| User value | ✅ "Spam patterns silently dropped before api pipeline — user account doesn't reveal automated nature" |
| Independent | ✅ Backward deps on 15.01–15.03 only |
| No forward deps | ✅ Clean |
| Story sizing | ⚠️ Contains a cross-service refactor: `InboundRateLimitRepository` moved from `services/bot_gateway/` to `platform_common/`. This is appropriate (two consumers) but requires running the full test suite across `services/bot_gateway/`, `services/user_gateway/`, AND `platform_common/` after this story. |
| ACs testable | ✅ Comprehensive — all 6 filter types, ordering (first-match-wins), missing keyword file gracefully handled, DEBUG log content verified |
| NFR-15-05/09 | ✅ Explicit no-reply constraint documented in story with reminder comment instruction |

### Best Practices Compliance Checklist

| Epic 15 | Story 15.01 | Story 15.02 | Story 15.03 | Story 15.04 |
|---------|-------------|-------------|-------------|-------------|
| Delivers user value | ⚠️ infrastr. | ✅ | ✅ | ✅ |
| Independent of future work | ✅ | ✅ | ✅ | ✅ |
| Stories sized appropriately | ✅ | ⚠️ cross-svc | ✅ | ⚠️ refactor |
| No forward dependencies | ✅ | ✅ | ✅ | ✅ |
| DB tables created when needed | ✅ | ✅ | n/a | ✅ |
| Clear acceptance criteria | ✅ | ✅ | ✅ | ✅ |
| FR traceability maintained | ✅ | ✅ | ✅ | ✅ |

### 🔴 Critical Violations

None.

### 🟠 Major Issues

None.

### 🟡 Minor Concerns

**MINOR-1**: Story 15.01 is infrastructure-first (service skeleton with no business logic). Standard project pattern — not actionable.

**MINOR-2**: Story 15.02 spans two services (user_gateway auth + bot_gateway `/user_login`). The cross-service scope is intentional and appropriate but means a wider PR blast radius. Developer should ensure both `tests/bot_gateway/` and `tests/user_gateway/` pass before merging.

**MINOR-3**: Story 15.04's `InboundRateLimitRepository` refactor to `platform_common/` will break `bot_gateway` tests that import from the old path until the import is updated. CI will catch this, but the developer must run the full test suite (not just `tests/user_gateway/`) before marking the story complete.

**MINOR-4**: The FR Coverage Map table in `epics.md` does not include Epic 15 rows. Non-blocking; recommend adding after the first story merges.

---

## Summary and Recommendations

### Overall Readiness Status

**✅ READY — Epic 15 is clear to proceed to implementation.**

All 20 FRs and 9 NFRs are fully covered across 4 coherent stories. No critical violations, no major issues, no forward dependencies, no unresolved architectural conflicts.

### Issues Found: 6 (0 Critical, 0 Major, 4 Minor, 2 Documentation Gaps)

| ID | Severity | Description | Blocking? |
|----|----------|-------------|-----------|
| GAP-1 | 🟡 Low | Epic 15 FRs/NFRs not in `PRD.md` — requirements live only in `epics.md` | No |
| GAP-2 | 🟡 Low | FR Coverage Map table in `epics.md` not updated with Epic 15 rows | No |
| MINOR-1 | 🟡 Minor | Story 15.01 is infrastructure-first with limited direct user value | No |
| MINOR-2 | 🟡 Minor | Story 15.02 spans two services (user_gateway + bot_gateway) — wider PR blast radius | No |
| MINOR-3 | 🟡 Minor | Story 15.04 `InboundRateLimitRepository` move requires full cross-service test suite run | No |
| MINOR-4 | 🟡 Minor | FR Coverage Map in `epics.md` does not include Epic 15 entries | No |

### Critical Issues Requiring Immediate Action

None. All issues are non-blocking.

### Recommended Next Steps

1. **Proceed to Sprint Planning** — `bmad-sprint-planning` [SP] to produce an ordered implementation plan for Epic 15 stories 15.01 → 15.02 → 15.03 → 15.04. Epic 12 must close out first (feature-sequential rule).

2. **Amend `PRD.md` (non-blocking, low priority)** — Add a Feature Group section for Epic 15 (FR-15-01 through FR-15-20, NFR-15-01 through NFR-15-09) matching the Epic 11/13/14 section pattern. This restores full traceability from PRD to stories. Can be done as a housekeeping PR separate from implementation.

3. **Developer alert for Story 15.04** — When implementing Story 15.04, run the full test suite across `services/bot_gateway/`, `services/user_gateway/`, AND `platform_common/` after moving `InboundRateLimitRepository`. The `bot_gateway` import update is easy to miss.

4. **Update FR Coverage Map after Story 15.01 merges** — Add the Epic 15 rows to the coverage map table in `epics.md` as a housekeeping commit.

### Architecture Alignment (Epic 15)

Epic 15 is architecturally clean:
- New service follows existing `create_service_app` + `platform_common/settings.py` patterns exactly
- `AuthSessionRepository` follows the established `*Repository` + SQLite singleton pattern
- `asyncio.Queue` + drain worker follows the `bot_gateway` inbound pattern
- `InboundRateLimitRepository` move to `platform_common/` is the correct long-term home (two consumers)
- No new external dependencies beyond Telethon + qrcode[pil] (already evaluated in technical research)
- Security constraints (NFR-15-03/04/05) are explicit, verifiable, and traceable to story test cases

### Final Note

This assessment identified **6 issues** across **2 categories** (documentation gaps, minor story observations). No blocking issues exist. Epic 15 stories are logically sequenced, independently completable, and fully traceable to their requirements. The planning artifacts are ready for implementation.

**Report generated**: `_bmad-output/planning-artifacts/implementation-readiness-report-2026-06-09.md`
**Assessor**: Implementation Readiness Check (bmad-check-implementation-readiness)
**Date**: 2026-06-09
