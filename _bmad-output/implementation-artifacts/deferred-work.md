# Deferred Work

## Deferred from: code review of 14-03-message-volume-instrumentation (2026-06-12)

- `asyncio.Queue` replaced at `UsageRecorder.start()` — any items queued between import and the startup hook are silently discarded. Pre-existing in Story 14-02's recorder design. Consider initializing queue lazily only in `start()` in a future story.
- `bootstrap_usage_db` crash risk if `.data/` directory is missing at process start in bot_gateway (identical risk already present in api service). Docker Compose handles directory creation; consider adding a graceful mkdir-p guard before the connect call.
- Sync `ensure_default_project()` SQLite call inside async helpers (`_enqueue_inbound_customer_message`, `_enqueue_inbound_operator_message`) without `asyncio.to_thread`. Pre-existing bot_gateway pattern. Could be wrapped in `asyncio.to_thread` when the bot_gateway is refactored to consistently use thread-dispatched sync I/O.
