---
stepsCompleted: [step-01-document-discovery, step-02-prd-analysis, step-03-epic-coverage-validation, step-04-ux-alignment, step-05-epic-quality-review, step-06-final-assessment]
date: '2026-06-19'
project_name: Semantaix
assessor: BMAD check-implementation-readiness (whole-project review)
scope: Whole-project readiness — PRD / Architecture / Epics / Stories vs as-built code @ origin/main 475b3d6
documents:
  prd: PRD.md (+ prd-addenda/epic-16-operator-self-registration.md)
  architecture: architecture.md
  epics: epics.md + epics/ (epic-01 … epic-16)
  stories: implementation-artifacts/ (110 docs) + sprint-status.yaml
  ux: none (backend / Telegram platform — no UI design docs; expected)
---

# Implementation Readiness Assessment Report

**Date:** 2026-06-19
**Project:** Semantaix
**Reviewer role:** Product Manager — requirements traceability & planning-gap detection

> **Framing.** This is a *brownfield* readiness check. The usual question ("is planning complete
> enough to start coding?") is mostly **already answered "yes"** — epics 01–13 and 16 are shipped.
> The risk has inverted: **the code has run ahead of the planning/status artifacts**, and the
> status trackers contradict each other. The findings below are about *traceability integrity and
> tracker truthfulness*, which is what governs whether the **next** epics (14 completion, 15) can be
> safely started.

## Step 1 — Document Discovery

| Type | Found | Notes |
|---|---|---|
| PRD | `PRD.md` (84 KB) + `prd-addenda/epic-16-...md` | No whole/sharded duplicate. ✅ |
| Architecture | `architecture.md` (29 KB) | Single, "as-built". ✅ (but stale — see F-1) |
| Epics | `epics.md` (53 KB) + `epics/` (16 files) | Status table present (stale — see F-2) |
| Stories | `implementation-artifacts/` (110) + `sprint-status.yaml` | Authoritative-ish (inconsistent — F-2/F-3) |
| UX | **none** | Expected — backend/Telegram platform, no GUI. Not a gap. |

No unresolved duplicate-format conflicts. Prior readiness reports exist through 2026-06-16.

## Step 2 — PRD / Requirements Traceability (FR-1 … FR-36, NFR-1 … NFR-16)

PRD defines **FR-1 … FR-36** + **NFR-1 … NFR-11/16**. Epic-15 and Epic-16 requirement groups are
**not backfilled into `PRD.md`** — they live in `prd-addenda/epic-16-...md` (FR-16-01…16-16) and in
the `epics.md` Requirements Inventory (Epic 15). This is an **acknowledged** traceability gap (noted
in the 2026-05-24 and 2026-06-16 reports) but remains **open**.

| FR group | Theme | Epic | As-built status |
|---|---|---|---|
| FR-1 … FR-14 | Core answer / RAG / HITL / guardrails / incidents | 01–07 | ✅ Shipped |
| FR-15 … FR-17 | Tenant knowledge ops + traces | 08 | ✅ Shipped |
| FR-18 … FR-22 | Calendar availability & scheduling | 11 | ✅ Shipped |
| FR-23 … FR-25 | Unified project services catalog | 13 | ✅ Shipped |
| FR-26 … FR-36 | Usage / token / cost monitoring **+ cost-spike alerting** | 14 | ⚠️ **Partial** — recording/API/dashboard done; **alerting (14-09) + signoff (14-10) backlog** |
| FR-16-01 … 16-16 (addendum) | Operator self-registration & onboarding | 16 | ✅ Shipped — but PRD.md not backfilled |
| Epic-15 inventory | Telegram user-account gateway | 15 | ⚠️ **Code shipped out-of-band; epic+FRs untracked** (see F-3) |

**Conclusion:** the only PRD FR group not fully delivered is **FR-26…FR-36** (the marquee
"Cost-Spike Alerting" of Epic 14 is not implemented; usage *plumbing* is). All other FR groups are
in code. PRD↔code coverage is high; PRD↔planning *bookkeeping* is where the gaps are.

## Step 3 — Epic Coverage Validation (the core finding)

There are **three disagreeing sources of truth** for epic/story status:

1. `epics.md` status table
2. `implementation-artifacts/sprint-status.yaml`
3. the actual code on disk

### F-2 (HIGH) — `epics.md` status table contradicts `sprint-status.yaml` and the code

| Epic | `epics.md` table says | `sprint-status.yaml` says | Code on disk |
|---|---|---|---|
| 12 Sales | "In progress … stories backlog" | `epic-12: done` | `sales/` (5 repos) + 88 story artifacts → **shipped** |
| 14 Usage | "Planning (Step 2 … Step 3 next)" | `epic-14: in-progress` (14-01..07 done, 08 review, 09/10 backlog) | `usage/` + `/api/usage/*` + DB → **mostly shipped** |
| 15 user_gateway | "Planning (requirements confirmed)" | `epic-15: backlog` (all stories backlog) | full `user_gateway/` service (9 modules) → **shipped** |
| 16 Self-reg | "Shipped" | `epic-16: in-progress` (all stories done) | shipped |

No two columns agree for any of epics 12/14/15/16. A reader cannot determine true status from the
planning artifacts alone — they must read the code. This defeats the purpose of the status trackers.

### F-3 (HIGH) — Epic 15 (`user_gateway`) was delivered *through Epic 16*, leaving Epic 15 unaccounted

`epic-15` and all its stories (15-01 skeleton, 15-02 QR auth, 15-03 routing, **15-04 spam filters**,
**15-05 cross-channel dedup**) are `backlog`, with **zero implementation artifacts**. Yet the
`user_gateway` service fully exists (Telethon QR/2FA auth, `message_router.py`,
`operator_client_pool.py`, `semantaix_user_gateway.db`). It was built under **Epic 16 stories
16-06-per-operator-user-gateway and 16-08-operator-customer-chat-channel** (both `done`).

Consequences:
- Violates the **feature-sequential "one epic at a time"** hard rule (`epics/README.md`): Epic 15
  content shipped while Epic 15 sits behind Epic 14 in the backlog.
- Epic 15's *own* safety stories — **15-04 spam filters** and **15-05 cross-channel dedup** — are
  **not obviously implemented** in the shipped service. A customer-facing MTProto channel without
  spam filtering / dedup is a live risk that no story currently tracks. **→ Phase 3/4 should verify
  whether `message_router.py` does any spam/dedup, since no story claims it.**

### F-4 (MEDIUM) — Epic 12 foundational stories marked `backlog` though the epic is `done`

In `sprint-status.yaml`, `epic-12: done`, and refinement stories 12-09…12-103 are `done`, but the
**foundational** stories they depend on — **12-01 (sales DB schema), 12-02 (service commands),
12-03 (persona answerer), 12-04…12-08** — are all still `backlog`, with no implementation artifacts
(the artifacts directory starts at 12-09/12-10). The code (`sales/` repos, `SalesPersonaAnswerer`
wired at `main.py:607`) proves the foundation shipped. The tracker rows were never flipped. Result:
the dependency graph in the tracker is internally inconsistent (done stories depending on backlog
stories) and cannot be trusted for "what's actually built."

### F-5 (MEDIUM) — Epic 14 incomplete but adjacent prerequisite closed

`14-07-usage-api-and-money-rbac: done` and `14-08-usage-bot-command: review` both depend on Epic
10.5 (`epic-10-5: done` ✅ — prerequisite satisfied, good). However **14-09 (alerting + incident
state machine)** and **14-10 (signoff)** are `backlog`. Epic 14's headline deliverable —
*cost-spike alerting* (FR-26…FR-36, NFR-8…NFR-11) — is therefore **not done**, while the epic is
already partially in production via the dashboard/API. Starting Epic 15 before 14-09/14-10 close
would again break feature-sequential discipline.

## Step 4 — UX Alignment

**N/A.** No UX design documents exist, which is correct for a Telegram-bot + admin-shell backend.
The only "UX" surface is the `web_ui` admin shell (37 routes) and Telegram message copy. One
adjacent note: customer-facing Russian copy correctness is governed by data files + the
"no presumed confirmation" rule (see memory) — not a UX-doc gap, but worth a copy audit in Phase 3.

## Step 5 — Epic / Story Quality Review

- ✅ **Story granularity is excellent** — one-PR-per-story is followed; epic 12 has 88 tightly
  scoped stories with signoff artifacts. This is a strength.
- ✅ **Dependency ordering is documented** (`epics/stories/epic-12/README.md` etc.).
- ⚠️ **Status hygiene is the weak point** — trackers are not updated when code merges (F-2/F-4),
  and cross-epic delivery is not reflected back into the originating epic (F-3).
- ⚠️ **Architecture doc drift** (F-1, carried from Phase 1): planning `architecture.md` still
  describes a single-answerer pipeline (code has 4: Sales→Calendar→GroundedRag→ScopeGuard), marks
  epics 11/13 "planned" (shipped), and lists 13 SQLite stores (code has 19; `user_gateway`,
  `rate_limits`, `webhook_dedup`, `sales`, `usage`, `calendar` missing). README still says "five
  services / placeholder bot_gateway".

## Summary and Recommendations

### Overall Readiness Status

**NEEDS WORK (bookkeeping), READY (code).** The *implementation* is in strong shape — FR coverage
is near-complete and code quality discipline (tests at 100%, one-PR-per-story) is high. The
*planning/status artifacts* are **not trustworthy** and must be reconciled before the next epic
starts, or feature-sequential discipline will keep silently breaking.

### Critical Issues Requiring Immediate Action

1. **F-3 (HIGH):** Verify Epic 15 spam-filter (15-04) and cross-channel-dedup (15-05) behavior in
   the shipped `user_gateway` — a live customer MTProto channel with no tracked spam/dedup story is
   a real safety gap. If unimplemented, raise stories now; if implemented under 16-06/16-08, mark
   15-04/15-05 done and close Epic 15.
2. **F-2 (HIGH):** Reconcile the three status sources. Make `sprint-status.yaml` the single source
   of truth, regenerate the `epics.md` table from it, and stop hand-maintaining a second table.
3. **F-5 (MEDIUM):** Decide explicitly whether to finish Epic 14 (14-09 alerting, 14-10 signoff)
   *before* opening Epic 15 — the marquee "cost-spike alerting" is unshipped.

### Recommended Next Steps

1. Flip Epic 12 foundational stories (12-01…12-08) to `done` (or document why they were folded into
   later stories) so the dependency graph is consistent (F-4).
2. Refresh `planning-artifacts/architecture.md` and `README.md` to the as-built reality
   (4-answerer pipeline, 19 stores, 6 services incl. `user_gateway`, epics 11/13 shipped) — or link
   `docs/architecture.md` (this review's as-built deltas) as the authoritative supplement (F-1).
3. Backfill PRD.md (or a clearly-linked addendum index) with the Epic-15 and Epic-16 FR groups so
   FR traceability is complete end-to-end.

### Final Note

This assessment identified **5 findings across 4 categories** (1× FR-coverage gap, 2× HIGH
status/traceability integrity, 2× MEDIUM tracker/doc drift). None block *current* production —
the code is ahead of the paperwork — but **F-3 carries real product risk** (untracked spam/dedup on
a customer channel) and is the first thing Phase 3/4 should verify in code. Address F-2/F-3 before
starting the next epic.
