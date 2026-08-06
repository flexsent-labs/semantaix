# Investigation: Repeated calendar connection notification

## Hand-off Brief

1. **What happened.** The operator reports that `✅ Календарь подключён...` is
   still delivered after bot restarts.
2. **Where the case stands.** The local source has no startup sender for this
   text; the persistent local calendar DB says project 1 is already connected
   to `@flexsentlabs`. A stronger persistent notification claim is needed to
   cover repeated/concurrent OAuth callbacks and any state drift.
3. **What's needed next.** Add and test an atomic, SQLite-backed one-time
   notification claim, reset only on explicit calendar disconnect, then restart
   the local services and verify the callback path does not send again.

## Case Info

| Field            | Value |
| ---------------- | ----- |
| Ticket           | N/A |
| Date opened      | 2026-08-05 |
| Status           | Concluded |
| System           | macOS, Python 3.11, local FastAPI API + Telegram bot gateway + ngrok |
| Evidence sources | User report, source code, SQLite state, local health checks, Git history |

## Problem Statement

The operator says the calendar connection confirmation is sent every time the
bot is restarted, although it should be sent only when the calendar is newly
connected.

## Evidence Inventory

| Source   | Status | Notes |
| -------- | ------ | ----- |
| User report | Available | Repeated notification after bot restart. |
| API source | Available | The exact text is sent only from the OAuth callback in `services/api/app/main.py`. |
| Bot gateway startup source | Available | Startup handlers sync commands and recover message queues; none sends the exact text. |
| Calendar SQLite state | Available | `.data/semantaix_calendar.db` has project 1 enabled and token status `connected` for `@flexsentlabs`. |
| Telegram runtime logs | Partial | Local PTY logs show health traffic but no persisted send audit for direct OAuth DMs. |
| Current Telegram webhook | Available | Webhook points to the local ngrok endpoint; Telegram reports no pending updates. |
| Deployed server state | Missing | No server endpoint/logs were provided in this turn. |

## Investigation Backlog

| # | Path to Explore | Priority | Status | Notes |
| - | --------------- | -------- | ------ | ----- |
| 1 | Trace exact notification senders | High | Done | Only the OAuth callback sends the exact confirmation. |
| 2 | Verify persisted connection state | High | Done | Local DB confirms connected state. |
| 3 | Make notification claim durable and atomic | High | Done | Protects restart/re-consent/concurrent callback paths. |
| 4 | Restart local stack and inspect health/logs | High | Done | Current API and bot gateway loaded the fix; health checks pass. |
| 5 | Verify any remote deployment | Medium | Blocked | Requires server access or its logs. |

## Timeline of Events

| Time | Event | Source | Confidence |
| ---- | ----- | ------ | ---------- |
| 2026-06-20 | `get_status()` replaced Fernet decryption for the existing-token guard. | commit `69601aa` | Confirmed |
| 2026-08-04 15:45 UTC | Project 1 token for `@flexsentlabs` was stored as `connected`. | `.data/semantaix_calendar.db` | Confirmed |
| 2026-08-05 06:31 local | API process was started and health checks passed. | process list / API PTY | Confirmed |
| 2026-08-05 | User reports notification still repeats after restart. | user report | Confirmed |

## Confirmed Findings

### Finding 1: The exact confirmation is not emitted by bot startup

**Evidence:** `services/api/app/main.py:4941-4948` defines the exact text and
`services/api/app/main.py:4989-5177` sends it only inside
`calendar_oauth_callback`; bot startup handlers are at
`services/bot_gateway/app/main.py:2349-2455` and do not reference this text.

**Detail:** A restart alone cannot produce this exact text through the current
local startup code. A callback/re-consent path, another process/deployment, or
an unavailable runtime state must be involved.

### Finding 2: The local persisted calendar is already connected

**Evidence:** `.data/semantaix_calendar.db` contains project `1`, enabled `1`,
operator `@flexsentlabs`, and token status `connected`.

**Detail:** The local DB has the state required for the existing callback guard
to suppress a repeat notification.

### Finding 3: Existing guard is state-based but not an atomic notification claim

**Evidence:** `services/api/app/main.py:5057-5072` checks token/settings before
upsert and `:5123-5133` skips when already connected; commit `69601aa` documents
that the guard was added to survive encryption-key rotation.

**Detail:** The check is not itself a durable record of having sent the DM and
two callbacks can observe the same pre-connect state before either upserts.

## Deduced Conclusions

### Deduction 1: Restart-only delivery is not explained by the local startup path

**Based on:** Findings 1 and 2.

**Reasoning:** The exact string has one local sender, and that sender is behind
the OAuth callback rather than a startup hook. The local database is already in
the connected state.

**Conclusion:** The reported restart event likely coincides with a callback or
uses a different/older runtime; a remote deployment remains unverified.

## Hypothesized Paths

### Hypothesis 1: Repeated OAuth callback is bypassing the existing guard

**Status:** Confirmed as a local correctness gap; the restart-only trigger remains unconfirmed

**Theory:** A callback reaches a process with missing/different state, or two
callbacks race before the token/settings state is updated.

**Supporting indicators:** The exact notification is callback-only; the current
guard performs a non-atomic read-before-write.

**Would confirm:** A `calendar_oauth_connected` log paired with a send on a
restart, including the callback's project/operator and runtime DB path.

**Would refute:** A Telegram send audit proving the exact text came from no
callback in the local or remote process.

**Resolution:** The durable claim is implemented and covered. The exact user-observed
restart trigger still needs remote logs if it reproduces after this local deployment.

## Missing Evidence

| Gap | Impact | How to Obtain |
| --- | ------ | ------------- |
| Exact Telegram message timestamp and source process | Cannot distinguish callback from remote deployment | Correlate Telegram chat timestamp with API logs and deployment logs. |
| Remote server runtime/DB state | Local evidence cannot prove the server is fixed | Inspect the server process and its calendar DB/logs. |

## Source Code Trace

| Element | Detail |
| ------- | ------ |
| Error origin | `services/api/app/main.py:5161`, `calendar_oauth_callback` sends `_CALENDAR_CONNECTED_DM`. |
| Trigger | Successful Google OAuth callback with a valid pending state and exchanged token. |
| Condition | Existing pre-upsert token/settings guard evaluates false, or concurrent callbacks race. |
| Related files | `services/api/app/calendar/token_repository.py`, `services/api/app/calendar/settings_repository.py`, `services/bot_gateway/app/main.py`. |

## Conclusion

**Confidence:** High for the local fix; Medium for the original trigger

Confirmed: the local startup code does not send the exact confirmation, the
local database is already connected, and the prior read-before-write guard had
no independent durable notification claim. The fix adds an atomic claim in the
calendar SQLite DB and clears it only on explicit disconnect. Remote/deployment
evidence is still missing, so a notification without a local OAuth callback
would point to an older/different runtime.

## Recommended Next Steps

### Fix direction

Implemented in `services/api/app/calendar/token_repository.py` and
`services/api/app/main.py`: a repository-owned durable claim keyed by
project/operator is claimed atomically before sending and cleared only on
explicit `/disconnect_calendar`. OAuth callback success remains independent of
Telegram delivery.

### Diagnostic

After the change, the local API and bot gateway were restarted; both health
checks and the ngrok health check returned OK, and startup logs contained no
calendar confirmation send. If a message still arrives without a local
`calendar_oauth_connected` event, investigate the remote deployment.

## Reproduction Plan

1. Use a temporary calendar DB and a first successful callback: one DM expected.
2. Recreate repositories and invoke a second successful callback: no DM.
3. Invoke two callbacks concurrently against the same project/operator: one DM.
4. Simulate explicit disconnect, then connect again: one new DM expected.
5. Restart the local services and verify no notification is emitted by startup.

## Final Verification

- Focused OAuth/repository/E2E tests: `48 passed`, calendar token repository
  coverage `100%`.
- Full test suite: `4112 passed`.
- Full repository coverage command reached `99.95%` because of 9 pre-existing
  misses in unrelated RAG/sales/usage code; the changed calendar repository and
  callback paths are fully covered.
- Ruff and `git diff --check`: passed.
- Live local API and bot gateway: healthy on ports 8000/8002; existing ngrok
  tunnel remained on `https://lustiness-apron-unmade.ngrok-free.dev`.

## Side Findings

- The active local Telegram webhook is `https://lustiness-apron-unmade.ngrok-free.dev/telegram/webhook`; Telegram reports zero pending updates but a stale 404 webhook error.
- The local pending-forward outbox contains unrelated test messages and no calendar confirmation text.

## Follow-up: 2026-08-06

### New Evidence

- The local running API and bot gateway use `/Users/aj/workspace_ai/semaintix` as
  their working directory and have been restarted with the follow-up code from
  local `main`.
- `.data/semantaix_calendar.db` contains project `1` enabled for
  `@flexsentlabs` and a token row with status `connected`; the durable
  `calendar_connect_notifications` table was empty because this connection
  predates the claim migration.
- The exact connected message still has one producer in
  `services/api/app/main.py:4942` and is only sent from the OAuth callback
  block; API and bot startup handlers do not reference it.

### Additional Findings

#### Finding 4: Legacy connected rows were not backfilled into the durable claim

**Evidence:** Before this follow-up, `calendar_oauth_callback` returned early
when `token_existed_before_upsert` or `project_was_enabled_before_upsert` was
true, before calling `claim_connection_notification` at
`services/api/app/main.py:5135`. The local database had the connected token but
no claim row.

**Detail:** Normal current code already suppresses the DM from a connected token
or enabled project. However, an older token-recovery process that deleted the
token could make a later callback look fresh again. The durable claim was not
recorded for legacy callbacks that were already suppressed.

### Updated Hypotheses

#### Hypothesis 1: Legacy state drift can re-arm the confirmation DM

**Status:** Confirmed as a residual local correctness gap; the exact remote
restart trigger remains unverified.

**Resolution:** The callback now claims the durable one-time marker before all
early-return guards. In addition, API startup backfills claims for existing
connected token rows without sending Telegram messages. A legacy connected
callback or restart therefore cannot re-arm that confirmation path unless the
operator explicitly disconnects the calendar.

### Backlog Changes

- Added a regression assertion that an already-connected callback creates the
  durable claim while still sending no Telegram DM.
- Added a startup migration regression test proving existing connected rows are
  claimed without a Telegram send, and that an unconfigured repository is
  skipped.
- Focused calendar verification: `62 passed`.
- Full repository verification: `4155 passed`, `100.00%` coverage, and `ruff`
  clean.

### Updated Conclusion

**Confidence:** High for the local residual fix; Medium for the original remote
trigger. Startup itself has no producer for the exact message: it only claims
legacy connected rows. The local callback now persists the one-time marker even
for legacy connected state, and the operator DM remains limited to a genuinely
unclaimed connection callback.
