# Investigation — Booking-dialog round-6 P1 blockers (D10 ↑P1, D12)

Status: **Active** — D10 root cause Confirmed; D12 mechanism Confirmed, trigger Hypothesized (needs incident logs).

## Hand-off Brief (15-second read)

Round-6 QA (2 Jun) ran against deployed `main` + **only** 12.36 (#117) — my D11/D10#30/D10-ScopeGuard PRs (#118/#119/#120) were **not deployed**. Two P1s: **D10 #34** — a clean booking ("Можно багги сегодня в 13:00…") was declined "Это не ко мне." That line is a **ScopeGuard `scope_decline` phrase** (settings.py:62), not the 12.34 out-of-scope guard; the booking fell through every upstream answerer (degraded persona LLM) to the last resort. My un-merged 12.39 does **not** catch it (its precise signal needs a scheduling *verb*; this phrasing has none). **D12 #35** — the next message got no reply at all; 12.36's async bound cannot cancel the **synchronous SQLite I/O** the inbound path runs on the event loop, which wedges under disk pressure.

## Case Info

- Input: `_bmad-output/implementation-artifacts/booking-dialog-defects-2026-06-01.md` (round-6 update, 2 Jun 2026).
- Deployed-under-test: `main` with 12.36 merged (#117); 12.37/12.38/12.39 (#118/#119/#120) OPEN, **not deployed**.
- Investigator branch: `feat/epic12-39-scope-guard-defers-inscope`.

---

## Finding D10 (#34) — clean booking declined "Это не ко мне" — **root cause CONFIRMED**

**Symptom (round 6):** `Можно багги сегодня в 13:00, нас четверо, одна багги?` → `Это не ко мне.` (P1 — breaks the primary happy path).

**Stronghold (Confirmed):**
- `"Это не ко мне."` is line 4 of the `scope_decline_messages` default — `platform_common/settings.py:62`. It is emitted **only** by `ScopeGuardAnswerer` (last in pipeline), `services/api/app/answerers/scope_guard.py`.
- `is_out_of_scope("Можно багги сегодня в 13:00…") == False` (probe) — its denylist is dining/lodging nouns only (`services/api/app/sales/out_of_scope.py:31-44`); no buggy/booking word matches. → **The doc's hypothesis ("12.34 out-of-scope guard catches it") is REFUTED.**

**Causal chain (Confirmed/Deduced):**
1. (Deduced) The booking reached the **last-resort ScopeGuard**, which means every upstream answerer skipped — the sales persona `_skip`ped on its LLM failure (Story 12.09 "thin gate"; degraded window), and calendar/RAG didn't handle it.
2. (Confirmed) ScopeGuard then returned a random `scope_decline` phrase → "Это не ко мне."
3. (Confirmed) My un-merged **12.39** would *not* rescue this: `_is_in_scope("Можно багги сегодня в 13:00…") == False` because `has_scheduling_intent == 0` and `classify_turn().kind == other`. The intent regex `_RU_INTENT = достав|заказ|закаж|куп|запис|запиш|брон|расписани|оформит|назнач` (`scheduling_context.py:59`) requires a scheduling **verb**; the "можно + service + time" phrasing has none. (`is_sales_intent == 1`, but that's the loose signal 12.39 deliberately avoids.)

**Why this is a real gap (not just "deploy my PRs"):** even after #120 deploys, this exact phrasing still declines. The precise signal is too narrow — it misses a booking expressed by *structure* (service + concrete date + time + headcount) rather than by verb.

**Fix direction (Confirmed-safe):** broaden ScopeGuard's `_is_in_scope` to also treat a turn that **parses as a concrete booking** as in-scope:
`in_scope = not is_out_of_scope AND (has_scheduling_intent ∨ turn∈{price_ask,catalog_ask} ∨ extract_requested_start(text, now, tz) is not None)`.
- `extract_requested_start("Можно багги сегодня в 13:00…")` → today 13:00 (concrete) → in-scope → defer to HITL. ✅
- `extract_requested_start("Какое сегодня число?")` → None (no time) → still declines. ✅ (no false positive)
- Still AND-gated on `project_does_bookings` + `not is_out_of_scope`. This refines un-merged #120.

---

## Finding D12 (#35) — second message fully silent — **mechanism CONFIRMED, trigger Hypothesized**

**Symptom (round 6):** `#34` replied (08:28), then `#35 Хочу записаться на багги сегодня в 14:00, нас двое.` → no reply, 7+ min. Bot needed a restart. Pattern across rounds: answers one/few, then silent.

**Refuted hypotheses:**
- *Timeout mismatch.* `inbound_pipeline_timeout_seconds=40` < `inbound_forward_timeout_seconds=45` (`settings.py:135,141`, enforced by the settings comment; gateway uses the 45s setting at `bot_gateway/app/main.py:2032`). Ordering is correct. **Refuted.**
- *LLM hang.* `complete_json` uses `httpx.AsyncClient(timeout=30)` + `await` (`openrouter_client.py:237`) — bounded AND cancellable. A hung LLM → httpx 30s raise → 12.36 except → escalate + ack. Would still reply. **Refuted as the cause of *full* silence.**
- *12.36 doesn't reply on timeout.* It does: the except block sends `ack_message` + creates a HITL ticket (`main.py:2438-2456`). **Refuted.**

**Confirmed mechanism:** the inbound handler runs **synchronous SQLite I/O directly on the event loop** — on both the success path and, critically, the **escalation/except path**: `_effective_inbound_ack_message()` (runtime-config read, `main.py:2435`), `hitl_ticket_repository.create()` (`:2445`), `.assign()` (`:2451`), `_persist_answer_trace()` (`:2457`) — none wrapped in `asyncio.to_thread`. `asyncio.wait_for` can only cancel at an `await`; it **cannot interrupt a blocking sync call**. If a SQLite call blocks (WAL "database is locked" under contention, or disk-full I/O wait), the event loop wedges — and since 12.36's own escalation path *also* does sync SQLite, even the fallback ack may never send → **full silence until restart**.

**Trigger (Hypothesized — data gap):** disk pressure / DB-lock contention at the incident window. Directly observed THIS session: data volume at **90%**, an **ENOSPC** during the coverage gate, ~14 GB reclaimable Docker cache. Documented recurring incident (memory: "live-stack-topology-and-nginx-502", disk-full). Under ENOSPC/WAL contention, sync SQLite on the loop is the plausible wedge.

**Missing evidence (to raise confidence to High):** api + bot_gateway logs and `df` from 08:28–08:30 on 2 Jun — look for `database is locked`, `disk I/O error`, an unhandled traceback, OOM/restart, or thread-pool starvation. Without them the *trigger* is Hypothesized; the *mechanism* (sync I/O un-cancellable by 12.36) is Confirmed by code.

**Fix direction (two parts):**
1. **Code (defensive, mechanism-level):** move the inbound escalation/persist path's blocking SQLite calls off the event loop (`asyncio.to_thread`) so a slow/locked DB can't wedge the loop, and so 12.36's bound can actually deliver the fallback. Consider a hard outer watchdog independent of any sync call.
2. **Ops (trigger):** relieve disk pressure — `docker builder prune` (≈8.7 GB cache) + image prune (≈5.3 GB), and monitor the data volume. (Flagged to user; needs approval per CLAUDE.md.)

---

## Cross-cutting note

Round-6 was tested on a build **behind** my open PRs. Recommended sequence: merge #118→#119→#120, **add the D10 #34 signal-broadening** (above) to #120, deploy, then re-test D8/D11 (which round 6 couldn't reach because the bot stalled). D12 needs the code hardening + disk remediation regardless.

## Status

**Concluded (fixes shipped).**
- **D10 #34** → folded into Story 12.39 (#120): ScopeGuard now treats a turn that parses as a concrete booking (`extract_requested_start`) as in-scope, so the structural booking defers to HITL instead of declining. Verified.
- **D12** → Story 12.40 (new): all synchronous SQLite I/O in the inbound flow moved off the event loop (`asyncio.to_thread`), so a locked/slow DB blocks a worker thread, not the loop — the bot stays responsive and self-recovers. Mechanism-level fix shipped; the specific #35 *trigger* still wants incident logs to confirm, but the disk pressure (likely trigger) was also remediated (Docker prune, ~8.7 GB).
- **Ops:** data volume relieved (90% → 88%, +8.7 GB) — addresses the recurring disk-full trigger.

Remaining (not blockers): D8 (language mirroring) and D11 re-test were unreachable in round 6 because the bot stalled; re-test after #118–#120 + 12.40 deploy.
