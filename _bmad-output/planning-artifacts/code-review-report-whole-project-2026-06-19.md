# Code / Architecture Audit — Whole Project

**Date:** 2026-06-19
**Target:** `origin/main` @ `475b3d6` (clean tree — audit of source, not a diff)
**Method:** BMAD `bmad-code-review` adversarial layers (Blind Hunter / Edge-Case Hunter /
Acceptance Auditor), applied by the lead reviewer to the highest-risk surfaces. **Scope note:** a
full 175-file sweep would use the skill's parallel-subagent fan-out; this pass deliberately targets
the safety-critical answer path, the newly-shipped `user_gateway` customer channel (flagged HIGH in
the Phase-2 readiness report), auth, and calendar OAuth. Other surfaces are **not** cleared.

## Triage summary

| ID | Severity | Bucket | Title |
|---|---|---|---|
| A1 | **HIGH** | patch | `user_gateway` mints a random `trace_id` per delivery → defeats api inbound idempotency (duplicate processing of re-delivered MTProto messages) |
| A2 | **HIGH** | decision_needed | `user_gateway` customer channel has no spam filter / rate limit / dedup (stories 15-04, 15-05 unimplemented & untracked) |
| A3 | **MEDIUM** | patch | Calendar OAuth `state.consume()` is a non-atomic SELECT-then-UPDATE despite "atomic single-use" docstring (concurrent double-consume race) |

No findings dismissed in the reviewed surfaces. The reviewed code is otherwise high quality —
strong per-layer failure discipline, injected clocks, hashed secrets, typed domain errors.

---

## A1 (HIGH, patch) — `user_gateway` random `trace_id` defeats api idempotency

**Location:** `services/user_gateway/app/message_router.py:64` →
`api_client.forward_inbound(trace_id=...)` → api inbound idempotency at
`services/api/app/main.py:2585`.

**Evidence.** The api dedups inbound on **`trace_id`** (`main.py:2585`: _"if we've already
processed this trace_id, return the cached…"_). The Bot-API path deliberately derives a
**deterministic** trace_id from the Telegram `update_id`
(`bot_gateway/app/main.py:2491 _derive_trace_id`, comment at :2495 — _"…collide on the same
trace_id, so api-side idempotency [works]"_; also `f"tg-update-{update_id}"` in the sales/material
dispatchers). The `user_gateway` path instead does:

```python
trace_id = str(uuid.uuid4())   # message_router.py:64 — fresh random per delivery
```

**Consequence.** Telethon re-delivers messages on reconnect / catch-up. Because each delivery gets a
**new** random trace_id, the api never recognizes the duplicate → the same customer message is
**fully re-processed**: a second LLM answer + second outbound reply to the customer, and potentially
a second HITL ticket and a second sales-state transition (`SalesPersonaAnswerer` mutates state).
This is the "cross-channel dedup" behavior that story **15-05** was supposed to provide.

**Fix (unambiguous — mirror the existing pattern).** Derive a stable trace_id from the Telethon
message identity, e.g. `f"tg-user-{operator_id}-{chat_id}-{message.id}"`, so re-delivery reuses the
same key and the api idempotency cache collapses the duplicate. (The Bot-API path is the reference
implementation.)

## A2 (HIGH, decision_needed) — `user_gateway` has no spam filter / rate limit / dedup

**Location:** `services/user_gateway/app/message_router.py` (whole file).

**Evidence.** `handle_new_message` filters only (a) non-private messages and (b) the operator's own
username; everything else is queued and forwarded to the LLM pipeline. There is **no** rate-limit
repository, **no** spam filter, **no** idempotency store in `services/user_gateway/` (grep returns
nothing for `rate_limit|dedup|spam|idempoten`). By contrast the Bot-API path has
`bot_gateway/app/rate_limit_repository.py` (106 LOC) **and** `webhook_dedup.py` (80 LOC) **and**
transcript `UNIQUE(source_message_id)`.

**Consequence.** A live, customer-facing MTProto channel (the operator's *personal* Telegram
account) will forward **every stranger's DM** straight into the grounded-RAG/LLM pipeline with no
throttle. A flood is (1) an unbounded LLM-cost amplifier and (2) a HITL-ticket / sales-state
amplifier. This compounds with the Phase-2 finding that Epic 14's **cost-spike alerting (14-09) is
not shipped** — the one safety net that would catch the cost blowup is also absent.

**Why decision_needed, not patch.** The correct policy (per-sender rate limit window? hard cap?
silent drop vs. polite throttle reply? reuse `bot_gateway`'s `RateLimitRepository` or a
`user_gateway`-local one?) is a product decision tied to stories 15-04/15-05, which the readiness
report (F-3) shows are **untracked**. Recommend: raise/own 15-04 + 15-05, reuse the
`RateLimitRepository` pattern, gate before `queue.put_nowait`.

## A3 (MEDIUM, patch) — OAuth `state.consume()` non-atomic despite docstring

**Location:** `services/api/app/calendar/oauth_state_repository.py:91-122`.

**Evidence.** The module docstring claims _"`consume` is atomic single-use."_ The implementation is
a **SELECT** (check `consumed_at IS NULL` and not expired) **then** a separate **UPDATE**, inside a
default-deferred SQLite transaction. Callbacks run concurrently via `asyncio.to_thread`. Two
requests presenting the same valid `state` can both execute the SELECT (both see `consumed_at IS
NULL`) before either issues its UPDATE — SQLite serializes the two writes but does not roll back the
stale read. The single-use guarantee then holds only for **sequential** replays, not concurrent ones.

**Consequence.** Defense-in-depth weakening of the CSRF single-use property: a concurrent
double-submit of one `state` could run the Google token exchange twice. Downstream impact is bounded
(the token upsert is idempotent on `(project_id, operator)`), so this is MEDIUM, not HIGH — but the
docstring overstates the guarantee.

**Fix (unambiguous).** Make consume atomic with a conditional write + rowcount check:

```sql
UPDATE calendar_oauth_pending_state
SET consumed_at = :now
WHERE state_hash = :h AND consumed_at IS NULL AND expires_at > :now
```

then treat `rowcount == 0` as `InvalidOAuthState` (disambiguate unknown/expired/consumed with a
follow-up SELECT only on the miss path).

---

## What was NOT reviewed (explicit gaps)

This pass covered ~5 files on the highest-risk paths. **Not** audited: the 115-route api surface
beyond inbound, `web_auth`/`admin_auth` session lifecycle internals, the four-layer grounding gate
internals (`grounded_rag.py`), the 88-story sales subsystem, backups/restore, knowledge moderation,
and `web_ui`. A complete audit should fan out the skill's parallel reviewers across these. Severity
ranking above is relative to what was reviewed.

## Resolution (2026-06-20)

All three audit findings fixed in the same review session; `ruff check .` clean and full suite
**4068 passed at 100% coverage**.

- **A1 — fixed.** `message_router._derive_trace_id` now derives `tg-user-{operator_id}-{chat_id}-
  {message.id}` (uuid fallback when no id), restoring api idempotency on re-delivery. Tests:
  `test_message_router_derives_deterministic_trace_id`, `..._falls_back_to_uuid`.
- **A2 — fixed.** `InboundRateLimitRepository` promoted to `platform_common/inbound_rate_limit.py`
  (bot_gateway keeps a re-export shim) and injected into `MessageRouter`; over-budget customer DMs
  are dropped before enqueue (channel parity with the Bot-API path). New setting
  `user_gateway_rate_limit_db_path`. Tests: `..._rate_limited_drops`, `..._allows_within_budget`.
- **A3 — fixed.** `oauth_state_repository.consume()` is now a single conditional `UPDATE … WHERE
  consumed_at IS NULL AND expires_at > :now` with a `rowcount` check — genuinely atomic single-use;
  the miss path disambiguates unknown/consumed/expired. All four existing tests still green.

## Recommendation

Patch **A1** immediately (one-line-class fix, mirrors existing code, removes a live double-billing /
double-action bug). Make a product call on **A2** (it gates safe operation of the per-operator
channel) and schedule **A3** with the next calendar touch. A1 + A2 both trace directly to the
untracked Epic-15 stories 15-04/15-05 from the Phase-2 readiness report — closing them closes both.
