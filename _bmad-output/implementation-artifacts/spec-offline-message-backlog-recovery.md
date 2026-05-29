---
title: 'Offline message backlog recovery (collapse redelivered burst to latest)'
type: 'feature'
created: '2026-05-29'
status: 'done'
context: ['{project-root}/_bmad-output/project-context.md']
baseline_commit: '44485fd879aa2eb29bd04abfce42fbc9fe2dea5c'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** When bot_gateway is down (server restart, deploy, crash), Telegram queues customer messages and redelivers the whole backlog once the webhook endpoint recovers. Each redelivered message currently flows independently to `/conversations/inbound`, so one customer who sent N messages during downtime gets up to N separate answers/escalations — spam, and the answer pipeline never sees the messages as a coherent conversation.

**Approach:** Lean on Telegram's automatic webhook redelivery. Tag each customer message with its Telegram send time; treat messages whose send time is older than a staleness threshold as "backlog" and divert them into a per-conversation SQLite buffer (mirroring `MediaGroupBuffer`) instead of forwarding immediately. A debounced flush waits for the redelivery burst to settle, then answers **only the latest** message per conversation. If that latest message is too thin to stand alone (length + intent heuristic), prepend the immediately preceding buffered messages as inline context in the single `/conversations/inbound` call. Fresh (live) messages keep their current immediate-forward path untouched.

## Boundaries & Constraints

**Always:**
- Detect backlog via Telegram message `date` (epoch seconds) vs injected `now`; capture `date` in `NormalizedTelegramMessage`.
- Buffer is per `chat_id`, SQLite-backed in `persistence_db_path`, surviving a restart mid-debounce; dedup redelivered duplicates by `update_id` (`INSERT OR IGNORE`); first insert for a chat schedules the flush exactly once — mirror `MediaGroupBuffer` semantics.
- Exactly one `/conversations/inbound` call per conversation per flushed burst, for the latest message only.
- All new tunables live in `Settings` + `.env.example`; time is injected (`now`), never ambient `datetime.now()` in branch logic.
- Reuse `_forward_inbound_safe` for the flush forward so existing error/idempotency handling applies.

**Ask First:**
- Adding any field to the api `InboundMessageRequest` / `AnswerContext` (i.e. changing the api side). Default scope is bot_gateway-only with context passed inline in `text`.

**Never:**
- No `getUpdates` / `deleteWebhook` polling (incompatible with active webhook; out of scope).
- Do not answer preceding backlog messages individually; they are context only.
- Do not change the live (fresh-message) path behavior.
- No new SQLite DB file — add a table to the existing persistence DB.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Live customer message | `now - date <= stale_seconds` | Forward immediately (current behavior, unchanged) | N/A |
| First stale message in burst | `now - date > stale_seconds` | Add to backlog buffer, schedule debounced flush, return 200 accepted | N/A |
| Burst settles, latest self-contained | buffer has A,B,C; C length ≥ `min_context_chars` and not a context cue | Drain; forward ONLY C as `text`; A,B not answered | N/A |
| Burst settles, latest thin | buffer has A,B,C; C short or a cue word (e.g. "да", "сколько?") | Prepend up to `max_context_messages` preceding (B,A) as labeled context; forward one combined inbound | N/A |
| Single stale message, no preceding | buffer has one row | Forward that one as-is | N/A |
| Duplicate redelivery | same `update_id` already buffered | `INSERT OR IGNORE`; do not double-schedule | N/A |
| Two chats backlogged | independent buffers per `chat_id` | Each chat flushed to its own single answer | N/A |
| Flush forward fails | api 5xx/unreachable | Delegate to `_forward_inbound_safe` (logged, no crash) | existing safe-forward path |

</frozen-after-approval>

## Code Map

- `services/bot_gateway/app/telegram_update.py` -- adds `date: int | None` (Telegram epoch) to `NormalizedTelegramMessage` + extracts `message["date"]` in `normalize_update` **leniently** (missing/non-int → `None`, treated as live; chosen so the ~20 existing date-less test payloads + the live path stay unchanged — strict raising would have broken them and an epoch-0 default would mis-classify everything as backlog).
- `services/bot_gateway/app/offline_backlog_buffer.py` -- NEW. SQLite buffer mirroring `media_group_buffer.py`: `add()` (returns True on first row per chat), `latest_received_at()`, `drain()` (atomic read+delete, ordered by `update_id`). Table `offline_backlog_buffer` keyed `(chat_id, update_id)` storing `text`, `source_message_id`, `customer_username`, `message_date`, `received_at`; lives in `persistence_db_path`.
- `services/bot_gateway/app/offline_context.py` -- NEW. Pure helpers: `is_stale(message_date, now, stale_seconds)` (None → False), `is_thin(text)` (stripped length < `min_context_chars` OR exact/first-word context-cue match), `build_inbound_text(latest, preceding)` (latest alone, or labeled context block + latest), `load_context_cues()` (owns its default repo-root data path, override param, lru-cached).
- `data/russian_context_cues.txt` -- NEW (repo-root data dir, matching `russian_kb_intent_phrases.txt`). Short context-dependent RU/EN cues (data, not code), one per line; `#` comments.
- `services/bot_gateway/app/main.py` -- module singletons `offline_backlog_buffer` + `offline_context_cues`; in the customer branch: if `is_stale`, buffer + (on first row) schedule `_flush_offline_backlog_after_debounce` and return `status=backlog_buffered`. Flush polls `latest_received_at` until quiet ≥ `debounce_seconds` or settling cap, then `drain`, picks latest by `update_id`, builds text (context only when thin), calls `_forward_inbound_safe` with `_derive_trace_id`. Fresh path unchanged. A `@app.on_event("startup")` sweep (`_recover_offline_backlog_on_startup`) reschedules a flush for every `pending_chat_ids()` — this is what truly satisfies AC#3, since `BackgroundTasks` are in-memory and a restart mid-debounce would otherwise strand the SQLite rows.
- `platform_common/settings.py` + `.env.example` -- add `offline_backlog_stale_seconds` (30), `offline_backlog_debounce_seconds` (5), `offline_backlog_settling_cap_seconds` (30), `offline_backlog_poll_interval_seconds` (0.5), `offline_backlog_min_context_chars` (12), `offline_backlog_max_context_messages` (3). (Cap + poll added to mirror the media-group debounce so a pathological stream can't delay a flush forever.)

## Tasks & Acceptance

**Execution:**
- [x] `services/bot_gateway/app/telegram_update.py` -- add + extract `date` leniently (`int | None`, missing/non-int → `None` = live) -- backlog detection needs send time without breaking date-less payloads.
- [x] `platform_common/settings.py` + `.env.example` -- add six tunables (stale/debounce/cap/poll/min-chars/max-context) -- no magic values.
- [x] `services/bot_gateway/app/offline_backlog_buffer.py` -- new SQLite buffer mirroring MediaGroupBuffer -- per-chat collapse that survives restart.
- [x] `data/russian_context_cues.txt` + `services/bot_gateway/app/offline_context.py` -- cue data + stale/thin-detection/text-building helpers -- intent heuristic + inline context, data-not-code.
- [x] `services/bot_gateway/app/main.py` -- staleness branch + debounced flush wiring; reuse `_forward_inbound_safe` -- the recovery behavior.
- [x] `tests/` (`test_offline_backlog_buffer.py`, `test_offline_context.py`, `test_telegram_update.py` date cases, `test_bot_gateway_offline_backlog.py` webhook+flush) -- cover every I/O Matrix row + each branch -- 100% gate. Also re-stamped 3 pre-existing fixture-dated tests with a fresh `date`.

**Acceptance Criteria:**
- Given bot was offline and 3 messages were redelivered for one chat, when the debounce window settles, then exactly one `/conversations/inbound` call is made for that chat and it carries the latest message.
- Given the latest backlog message is a short cue ("да"), when flushed, then the forwarded `text` includes the preceding buffered messages (capped at `max_context_messages`) as labeled context plus the latest.
- Given backlog rows exist and bot_gateway restarts mid-debounce, when it comes back, then the SQLite-persisted backlog is still drained (no lost messages).
- Given two different chats are backlogged, when flushed, then each chat receives its own single, independent answer.

## Design Notes

Inline context (not an api field) keeps blast radius to bot_gateway and honors "endpoints thin". Format the combined `text` so the pipeline can tell context from question, e.g.:

```
Предыдущие сообщения (контекст):
- Здравствуйте, у вас есть стрижка?
- А сколько стоит?
Вопрос клиента: да
```

Flush/debounce reuses the proven `MediaGroupBuffer` shape: background task polls `latest_received_at`, drains once quiet. Staleness threshold (30s) sits comfortably above sub-second live webhook latency but below any real downtime, so live traffic never diverts.

## Verification

**Commands:**
- `ruff check .` -- expected: no errors
- `pytest --cov --cov-config=.coveragerc --cov-report=term-missing` -- expected: all pass, 100% on `platform_common/` + `services/`
- `pytest tests/test_offline_backlog_buffer.py tests/test_offline_context.py -v` -- expected: green

## Suggested Review Order

Read in this order — each layer builds on the one above, so the design reads top-down.

1. **Intent & contract** — [spec Intent + I/O Matrix](_bmad-output/implementation-artifacts/spec-offline-message-backlog-recovery.md:12) — what "collapse the redelivered burst to the latest message" means and every edge case it must cover.
2. **Send-time capture** — [telegram_update.py:normalize_update](services/bot_gateway/app/telegram_update.py) — the lenient `date: int | None` extraction that classifies a message as live vs. backlog (start here; everything keys off it).
3. **Detection heuristics (pure)** — [offline_context.py](services/bot_gateway/app/offline_context.py) — `is_stale`, `is_thin`, `build_inbound_text`, `load_context_cues`; paired with [data/russian_context_cues.txt](data/russian_context_cues.txt). Easiest to reason about in isolation.
4. **Persistence** — [offline_backlog_buffer.py](services/bot_gateway/app/offline_backlog_buffer.py) — the per-chat SQLite buffer (`add`/`latest_received_at`/`drain`/`pending_chat_ids`); confirm `BEGIN IMMEDIATE` serialization and the first-row-schedules-once semantics.
5. **Orchestration** — [main.py](services/bot_gateway/app/main.py) — the customer-branch staleness divert, `_flush_offline_backlog_after_debounce`, and the `_recover_offline_backlog_on_startup` sweep that ties persistence to AC#3.
6. **Config surface** — [settings.py](platform_common/settings.py) + [.env.example](.env.example) — the six `offline_backlog_*` tunables.
7. **Tests** — [test_offline_context.py](tests/test_offline_context.py), [test_offline_backlog_buffer.py](tests/test_offline_backlog_buffer.py), [test_bot_gateway_offline_backlog.py](tests/test_bot_gateway_offline_backlog.py) — one per layer; the last covers the webhook branch, flush coroutine, and startup recovery end-to-end.
