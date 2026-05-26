# Sprint Change Proposal — 2026-05-26

**Author:** Aj (via `bmad-correct-course`)
**Trigger:** [`brainstorming-session-2026-05-26-1500.md`](../brainstorming/brainstorming-session-2026-05-26-1500.md) — Epic 14 planning surfaced two cross-epic amendments that must land before Epic 14 stories are drafted.
**Mode:** Incremental (per-edit review, completed in-session).
**Scope classification:** Moderate (backlog reorganization + open-PR amendment + new backlog epic; no code changes).

> **Numbering note:** During this session the proposed Usage Monitoring epic was renumbered **Epic 13 → Epic 14** because Epic 13 was claimed by a parallel session (YOLO runner). All references in this proposal use Epic 14.

---

## 1. Issue Summary

`bmad-brainstorming` for Epic 14 (Usage Monitoring + Cost-Spike Alerting) produced **45 locked design decisions** and **14 attack triages** (6 real, 8 noise). Two of the real-attack mitigations and one Phase-1 decision require artifact changes in epics **other than** Epic 14, and must be resolved before Epic 14's `bmad-create-epics-and-stories` runs cleanly.

**Two changes triggered:**

1. **Epic 10 — Operator-project model** lacks the flat-list-multi-operator-per-project model that Epic 14's `/usage` RBAC requires. The existing `hitl_primary_operator_username` indirection ties operator identity to a single env-configured user and blocks the operator-scoped project boundary that the dashboard, bot command, and API endpoints need (Phase 1 decision G3).
2. **Epic 12 PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) story 12-05b** (KB-upload → automatic client materials analysis) accepts unbounded KB uploads as auto-promoted materials. A moderator approving a batch of KB candidates triggers unbounded `client_materials_analyzer` LLM calls, producing cost spikes Epic 14's dashboard cannot meaningfully attribute (Phase 2 Attack 10).

**Issue type:** Misunderstanding/incompleteness of original requirements — not strategic pivot, not failed approach.

**Evidence:** `grep` confirms 8 production files reference `hitl_primary_operator_username`; PR #82 story 12-05b explicitly accepts any-volume KB uploads with no cap.

---

## 2. Impact Analysis

### Epic impact

| Epic | Current status | Action | Rationale |
|------|---------------|--------|-----------|
| Epic 10 | `done` (shipped) | **Not reopened.** Standalone **Epic 10.5** (`backlog`) added. | User decision (c). Avoids reopening shipped work; honors the natural Epic 14-needs-Epic 10.5 dependency direction. |
| Epic 12 | `in-progress` (planning-only in PR #82) | **Amend story 12-05b in PR #82 directly.** | User decision (a). PR is open and not merged; updating in-place is the cheapest path and keeps story 12-05b honest from day one. |
| Epic 14 | Not drafted | Will be drafted after Epic 10.5 lands and PR #82 is amended. | Hard rule: feature-sequential. Epic 14 queues behind Epic 12 + Epic 10.5 in `sprint-status.yaml`. |

### Artifact conflicts

| Artifact | Conflict | Resolution |
|----------|----------|------------|
| PRD ([`PRD.md`](PRD.md)) | None — post-MVP note already acknowledges project-scoping. | No edit. |
| Architecture ([`architecture.md`](architecture.md)) | None — zero `primary_operator` references. | No edit. |
| UI/UX | No deliverable. | No edit. |
| [`epics/README.md`](epics/README.md) | Lists Epic 11 as *(planned)* (stale); needs Epic 10.5 entry. | Edit. |
| [`sprint-status.yaml`](../implementation-artifacts/sprint-status.yaml) | Missing Epic 10.5 backlog block. | Edit. |
| [`e2e-coverage.md`](../implementation-artifacts/e2e-coverage.md) | Will need Epic 10.5 rows during implementation. | Deferred to Epic 10.5 story execution. |
| `platform_common/settings.py` + `hitl_runtime_config` table | Will lose two primary-operator fields/rows. | Deferred to Epic 10.5 story 10.5-01 implementation. |

### Technical impact

- **Code surface for Epic 10.5:** ~8 production files referencing `hitl_primary_operator_username` (settings.py, services/api/app/{main.py, admin_auth.py, operator_chat_lookup.py}, services/bot_gateway/app/{main.py, operator_resolver.py, calendar_commands.py}). One-shot SQLite migration removes two `hitl_runtime_config` rows.
- **Code surface for Epic 12 amendment:** Story-doc text only in this proposal. Code lands when story 12-05b is implemented as part of Epic 12's normal story-cycle.
- **No production downtime.** Epic 10.5 changes are additive at the DB layer (no schema drops) and refactor-only at the routing layer; behavior for the existing single-operator deployment is unchanged.

---

## 3. Recommended Approach

**Hybrid Direct Adjustment:**

- **Epic 12 → Option 1 (Direct Adjustment).** Amend story 12-05b in PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) directly with the four edits in §4.A below. Effort: **Low**. Risk: **Low** (PR is open, planning-only, single story file + one sibling-ripple note).
- **Epic 10 → "Add new epic" (Section 2.2 of checklist).** Standalone Epic 10.5 with `backlog` status, 4 stories, ready to enter implementation after Epic 12 ships. Effort: **Medium** (touches 8 files + migration). Risk: **Low–Medium** (no behavioral change for current single-operator deployment; brownfield refactor protected by existing 100% coverage gate).
- **Epic 14** queues behind Epic 10.5. Planning (next `bmad-create-epics-and-stories`) can proceed in parallel; implementation must wait per the feature-sequential rule.

**Why not rollback (Option 2):** Epic 10 is shipped in production; nothing to revert. The refinement is additive.

**Why not MVP review (Option 3):** Neither change alters MVP scope.

---

## 4. Detailed Change Proposals

### 4.A — Epic 12 PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) story 12-05b amendment

**Target file:** `_bmad-output/planning-artifacts/epics/stories/epic-12/story-12-05b-kb-upload-automatic-client-materials-analysis.md` on the PR's branch.

#### Edit A1 — Add cap rules to "In Scope" (insert three bullets after the `ClientMaterialsAnalyzer` block)

```
- **Hard cap of 20 active client materials per project.** Before registering, `ClientMaterialsAnalyzer.analyze_and_register` calls `materials_repo.count_active(project_id=...)`. If the count is >= 20, the analyzer SHORT-CIRCUITS the register step regardless of LLM verdict and returns `AnalysisOutcome(registered=False, material_id=None, reason="materials_cap_reached")`. The LLM analysis still runs (so the project's cost accounting captures the analyzer call; see Epic 14), but no `client_materials` row is written.
- **Operator ack on cap reached.** The bot_gateway hook, on receiving `AnalysisOutcome(registered=False, reason="materials_cap_reached")`, appends a SECOND line to the existing KB-upload ack: `📎 Слот материалов для клиентов занят (20/20). Удалите неиспользуемые материалы или используйте /material для замены.` This message is shown only when the cap is the reason; other `registered=False` cases stay silent (existing behaviour).
- **Cap is hard, not configurable.** The number 20 is a named constant `CLIENT_MATERIALS_CAP_PER_PROJECT` in `services/api/app/sales/client_materials_constants.py`. No `hitl_runtime_config` knob in this story.
```

**Rationale:** Without the cap, a moderator-driven batch approval triggers unbounded LLM analyzer calls. Epic 14's `call_outcome=moderation_triggered` chart assumes a bounded moderation surface; the cap is the contract that lets the dashboard distinguish moderator-driven cost from customer-driven cost.

#### Edit A2 — Extend "Out of Scope" (append two bullets)

```
- Making the 20-material cap configurable. A later epic may revisit this if real usage demands per-project tuning; v1 is a hard constant.
- Reclamation logic (auto-deleting old client_materials when the cap is hit). Operator must explicitly remove unused materials via `/material_remove` to free a slot.
```

#### Edit A3 — Extend "Test Plan / Unit + Integration"

**Append to Unit block:**

```
- `tests/test_client_materials_analyzer_cap_reached.py` — `materials_repo.count_active(project_id=...) -> 20` → analyzer SHORT-CIRCUITS register step; LLM call MAY OR MAY NOT have run (test fixture documents both shapes — analyzer impl chooses); returns `AnalysisOutcome(registered=False, reason="materials_cap_reached")`. No `materials_repo.add(...)` call is made.
- `tests/test_client_materials_analyzer_cap_boundary.py` — `count_active=19` allows one more registration; `count_active=20` blocks; `count_active=21` (data drift) also blocks (defensive).
```

**Extend Integration block:**

```
- `tests/test_bot_gateway_kb_upload_material_hook.py` — extend existing test: stub `ApiClient` returns `AnalysisOutcome(registered=False, reason="materials_cap_reached")` → operator receives KB ack followed by the cap message `📎 Слот материалов для клиентов занят (20/20). ...`. Distinct from the silent `not-sendable` case.
```

#### Edit A4 — Extend "Done Criteria" (append two bullets)

```
- 20-material-per-project cap enforced in `ClientMaterialsAnalyzer`; cap-reached path returns `materials_cap_reached` reason and the operator gets the Russian cap message in the ack.
- Cap constant lives in `services/api/app/sales/client_materials_constants.py`; no runtime config exposed.
```

#### Edit A5 — Add cross-story clarification to "Implementation Notes" (one-line insert)

After the `Auto-promotion respects the data-driven dormancy` bullet, insert:

```
- **The 20-material cap is materials-only.** Story 12-05c (services extraction) runs in parallel on the same KB upload but counts against a separate concern (`services` table); it has no cap. Do not unify the two enforcement paths.
```

---

### 4.B — Epic 10.5 standalone epic

**New file:** [`epic-10-5-operator-project-model-refinement.md`](epics/epic-10-5-operator-project-model-refinement.md) — written in full during Step 3 of this workflow. Contains:

- Goal: remove `hitl_primary_operator_username` indirection; enforce 1-op-per-project; rewrite bot gateway routing.
- 4 stories: 10.5-01 (settings/runtime cleanup + migration), 10.5-02 (bot gateway resolver rewrite), 10.5-03 (API surface updates), 10.5-04 (epic signoff + E2E).
- Exit criteria including 100% coverage and `scripts/epic10_5_signoff.sh`.
- Dependencies on Epic 10 (tables) and Epic 11 (calendar OAuth keeps working).
- E2E plan with 4 story-aligned tests under `tests/e2e/test_e2e_epic10_5_*.py`.

### 4.C — `epics/README.md` update

Update the epic-order list in [`epics/README.md`](epics/README.md):

**Old:**
```
9. `epic-09-operator-kb-growth.md`
10. `epic-10-multi-operator-projects.md`
11. `epic-11-calendar-availability-scheduling.md` *(planned)*
```

**New:**
```
9. `epic-09-operator-kb-growth.md`
10. `epic-10-multi-operator-projects.md`
10.5. `epic-10-5-operator-project-model-refinement.md` *(backlog — refinement to support Epic 14)*
11. `epic-11-calendar-availability-scheduling.md`
12. `epic-12-sales-conversation-persona.md` *(active — planning in PR #82)*
14. `epic-14-usage-token-cost-monitoring.md` *(planned)*
```

(Also corrects stale `(planned)` label on Epic 11, adds Epic 12 entry per PR #82, and pre-registers Epic 14. Epic 13 intentionally skipped per session-numbering decision.)

### 4.D — `sprint-status.yaml` update

Add a "Queued refinement" block after the Epic 11 done-block in [`sprint-status.yaml`](../implementation-artifacts/sprint-status.yaml):

```yaml
  # --- Active feature epic ---
  epic-12: in-progress  # planning-only in PR #82 at time of this proposal
  # Stories from PR #82 — status will flip to `ready-for-dev` per story as PR merges + cycle begins
  12-01-sales-db-schema-and-repositories: backlog
  12-02-service-commands-and-sales-state: backlog
  12-02b-natural-language-service-management: backlog
  12-03-sales-persona-answerer-greeting-and-intent: backlog
  12-04-pricing-turn-kb-first-escalate-if-unknown: backlog
  12-05-autonomous-client-materials-dispatch: backlog
  12-05b-kb-upload-automatic-client-materials-analysis: backlog
  12-05c-kb-upload-automatic-services-extraction: backlog
  12-06-service-list-and-concept-explainer: backlog
  12-07-date-proposal-turn: backlog
  12-08-proactive-followup: backlog
  12-09-pipeline-wiring-and-e2e-signoff: backlog
  epic-12-retrospective: optional

  # --- Queued refinement (blocker for Epic 14) ---
  epic-10-5: backlog
  10-5-01-settings-and-runtime-config-cleanup: backlog
  10-5-02-bot-gateway-resolver-rewrite: backlog
  10-5-03-api-surface-updates: backlog
  10-5-04-epic-signoff: backlog
  epic-10-5-retrospective: optional

  # --- Planned (post-10.5) ---
  epic-14: backlog
  epic-14-retrospective: optional
```

(Epic 12's stories were not previously tracked in `sprint-status.yaml` because PR #82 is open — capturing them here keeps the file canonical once #82 merges. Epic 14 entry is a placeholder; story rows will be filled by `bmad-create-epics-and-stories` after Epic 10.5 lands.)

---

## 5. PRD MVP Impact + High-Level Action Plan

**MVP impact:** None. Both changes are refinements to delivered MVP scope.

**Action plan + sequencing:**

```
NOW (this proposal):
  ├── Write Epic 10.5 doc                                       (DONE)
  ├── Write Sprint Change Proposal doc                          (DONE)
  ├── Update epics/README.md                                    (PENDING — see §6 handoff)
  ├── Update sprint-status.yaml                                 (PENDING — see §6 handoff)
  └── Apply 4.A edits to PR #82 story 12-05b                    (PENDING — see §6 handoff)

NEXT (Epic 12 implementation, no Epic 14 dependency yet):
  └── Epic 12 stories implemented per PR-per-story cycle
      (story 12-05b lands with cap baked in)

AFTER EPIC 12 SHIPS:
  └── Epic 10.5 stories implemented (10.5-01 → 10.5-02 + 10.5-03 → 10.5-04)

AFTER EPIC 10.5 SHIPS:
  ├── Run `bmad-create-epics-and-stories` for Epic 14
  └── Epic 14 stories implemented per PR-per-story cycle
```

**Dependencies summary:**

- Epic 14 → blocked by Epic 10.5 (RBAC) + Epic 12 cap (alerting attribution)
- Epic 10.5 → blocked by Epic 12 (feature-sequential one-at-a-time rule)
- Epic 12 → unblocked, in progress (PR #82 + per-story cycle)

---

## 6. Implementation Handoff Plan

### Scope classification: **Moderate**

Touches planning artifacts (Epic 10.5 doc, README, sprint-status), an open PR (story 12-05b), and queues a new backlog epic. No production code, no schema changes, no live-system impact.

### Handoff recipients

| Recipient | Responsibility | Deliverable |
|-----------|----------------|-------------|
| **Aj (this branch, `lucid-roentgen-1d6e00`)** | Commit Epic 10.5 doc + `epics/README.md` + `sprint-status.yaml` updates into a single PR titled `docs(bmad): epic 10.5 — operator-project model refinement + epic 14 placeholder`. | This PR. |
| **Aj or maintainer of PR [#82](https://github.com/flexsent-labs/semantaix/pull/82)** | Apply the 4.A edits (A1–A5) to story 12-05b on PR #82's branch. Force-push or stack commit per project convention. Update PR #82 description to note the cap addition. | Updated PR #82 awaiting review. |
| **Future `bmad-create-epics-and-stories` session** | After Epic 10.5 ships, draft Epic 14's stories using the brainstorming session's "Proposed Story Split for Epic 14" as the input. | Epic 14 story pack. |
| **Future `bmad-sprint-planning` session** | After Epic 14's stories exist, schedule the story-cycle. | Sprint plan. |

### Success criteria

- Epic 10.5 doc reachable from `epics/README.md`.
- `sprint-status.yaml` lists Epic 10.5 stories as `backlog`.
- PR #82 story 12-05b text contains the cap rule (verifiable by `grep "CLIENT_MATERIALS_CAP_PER_PROJECT" _bmad-output/planning-artifacts/epics/stories/epic-12/story-12-05b-*`).
- Brainstorming session doc and Epic 10.5 doc reference **Epic 14** (not Epic 13) — verified after the session-time renumbering.
- No code changes in this PR — purely planning artifacts.

### Out of band for this proposal

- The actual implementation of Epic 10.5 stories follows the standard `bmad-create-story` → `bmad-dev-story` → `bmad-code-review` cycle and is **not** delivered by this proposal.
- PR #82's story 12-05b edit, if applied via `gh pr checkout` on this machine, requires a separate commit on PR #82's branch (not this worktree's branch).

---

## Appendix — Checklist Status

| Section | Item | Status |
|---------|------|--------|
| 1 | 1.1 Triggering story | Done |
| 1 | 1.2 Problem statement | Done |
| 1 | 1.3 Evidence | Done |
| 2 | 2.1 Current epic completable | Action-needed (resolved by user decisions a/c) |
| 2 | 2.2 Epic changes | Done |
| 2 | 2.3 Future epics | Done |
| 2 | 2.4 New epics | Done (Epic 10.5 + Epic 14 placeholder) |
| 2 | 2.5 Reordering | Done |
| 3 | 3.1 PRD conflict | N/A |
| 3 | 3.2 Architecture conflict | Done (zero refs) |
| 3 | 3.3 UI/UX conflict | N/A |
| 3 | 3.4 Secondary artifacts | Done |
| 4 | 4.1 Option 1 Direct Adjustment | Viable (Epic 12) |
| 4 | 4.2 Option 2 Rollback | Not viable |
| 4 | 4.3 Option 3 MVP Review | Not viable |
| 4 | 4.4 Recommended path | Hybrid: Direct Adjustment (Epic 12) + Add new epic (Epic 10.5) |
| 5 | 5.1–5.5 Proposal components | Done (§§1–6 of this doc) |
| 6 | 6.1–6.5 Final review | Pending user approval |

---

## Postscript — Post-Approval State Reconciliation (2026-05-26, later in the day)

After this proposal was approved, the user reported that **PR [#82](https://github.com/flexsent-labs/semantaix/pull/82) had already merged** earlier the same day (merge commit `c2e88e7`, 2026-05-26T12:43:49Z). The amendment commit `9123eb0` had been pushed to the post-merge branch (`amazing-bardeen-775a7f`) and was therefore **not on `main`**.

Additionally, examining `main` revealed:

- **Epic 13** exists on `main` as a separate shipped epic — `epic-13-unified-project-services-catalog.md` (from PR [#80](https://github.com/flexsent-labs/semantaix/pull/80)). It is **not** a renumbering of Epic 12; Epic 12 (Sales Persona) remains intact at its original number.
- The user's earlier instruction "Epic 13 is being done by yolo runner in another session, renumber to 14" was correct — they were referring to PR #80's Unified Services Catalog, which legitimately occupies the Epic 13 slot. Our Usage Monitoring epic correctly stays at **Epic 14**.

### Post-approval actions taken

1. **Cherry-picked `9123eb0` onto a fresh branch off `main`** (`docs/epic-12-story-12-05b-materials-cap`) and opened **PR [#83](https://github.com/flexsent-labs/semantaix/pull/83)** — re-lands the cap edits on a clean base. PR is open at time of writing.
2. **Updated stale references** in this worktree's planning artifacts to reflect post-merge state:
   - [`epic-10-5-operator-project-model-refinement.md`](epics/epic-10-5-operator-project-model-refinement.md) — Epic 12 status updated; PR #82 / #83 / #80 cross-references added.
   - [`epics/README.md`](epics/README.md) — Epic 12 marked as planning-merged; Epic 13 (Unified Services) entry added; Epic 14 placeholder cleaned up.
   - [`sprint-status.yaml`](../implementation-artifacts/sprint-status.yaml) — Epic 13 done-block added with all 6 stories; Epic 12 status comment updated; Epic 14 comment cleaned.

### Updated handoff plan

| Recipient | Responsibility | Status |
|-----------|----------------|--------|
| Aj | Review + merge PR [#83](https://github.com/flexsent-labs/semantaix/pull/83) (cap commit re-landed on main). | **Open — awaiting review.** |
| Aj | Open a planning PR from this worktree (`lucid-roentgen-1d6e00`) containing Epic 10.5 doc + this proposal + README + sprint-status updates + brainstorming session. | Pending. |
| Future `bmad-create-epics-and-stories` session | Draft Epic 14 stories after Epic 10.5 ships. | Queued. |

No code changes in this reconciliation — purely planning-doc text updates.
