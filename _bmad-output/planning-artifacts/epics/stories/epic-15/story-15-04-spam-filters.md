# Story 15.04 — Spam Filters

## Objective

Implement all silent-drop spam filters so that scam accounts, fake accounts, bots, forwarded anonymous spam, rate-limited senders, URL floods, and keyword-matched messages never reach the `api` pipeline. The `user_gateway` never sends any reply — all drops are silent (the account must not reveal it is automated).

**As a** platform operator,
**I want** all known spam patterns silently dropped before reaching the answer pipeline,
**So that** the user account ignores scam/fake/bot senders, message floods, forwarded spam, and keyword-matched content without revealing its automated nature.

PRD reference: **FR-15-14** (scam/fake), **FR-15-15** (bot sender), **FR-15-16** (forwarded anonymous), **FR-15-17** (rate limit), **FR-15-18** (URL flood), **FR-15-19** (length truncation), **FR-15-20** (spam keywords), **NFR-15-05** (receive-only, no replies), **NFR-15-08** (DEBUG log drops), **NFR-15-09** (silent rate limit).

## Scope

### In Scope

- **`services/user_gateway/app/spam_filter.py`** — `SpamFilter` class:
  - Constructor: `SpamFilter(*, keyword_file_path: str, rate_limit_repo: InboundRateLimitRepository, settings: Settings)`
  - `is_spam(event, *, now: datetime) -> tuple[bool, str]`:
    - Returns `(True, reason_code)` if any filter matches; `(False, "")` if clean.
    - Filter order (first match wins):
      1. `sender.scam or sender.fake` → `"scam_fake"`
      2. `sender.bot` → `"bot_sender"`
      3. `message.fwd_from` set and `message.fwd_from.from_name` set (anonymous forward origin) → `"anonymous_forward"`
      4. Rate limit: `rate_limit_repo.check_and_record(chat_id=sender_id, now=now, max_messages=settings.inbound_rate_limit_messages, window_seconds=settings.inbound_rate_limit_window_seconds)` returns False → `"rate_limited"`
      5. URL count > 3: regex count of `https?://\S+` matches in `message.text` → `"url_flood"`
      6. Keyword match: any keyword from loaded keyword list appears (case-insensitive, whole-word boundary check) in `message.text` → `"spam_keyword"`
  - `_load_keywords(file_path: str) -> frozenset[str]`: reads the keyword file, strips blanks and `#` comment lines, lowercases. Loaded once at construction. Missing file → empty frozenset + WARNING log (never crashes the service).
  - Never logs message content; only `sender_id` and `reason_code` (NFR-15-08).

- **`services/user_gateway/data/spam_keywords.txt`** — initial keyword file, empty (one `# add keywords here, one per line` comment). Operators add keywords without code changes, mirroring `data/russian_hedges.txt`.

- **`services/user_gateway/app/message_router.py`** updates (wires the stub from 15.03):
  - `_is_spam(event) -> bool`:
    - `is_spam, reason = self.spam_filter.is_spam(event, now=datetime.now(UTC))`
    - If `is_spam`: log DEBUG `"user_gateway_spam_drop"` with `sender_id=event.sender_id`, `reason=reason`.
    - Return `is_spam`.
  - Length truncation before forwarding in `_forward`:
    - `text = (event.text or "")[:settings.inbound_max_message_chars]`

- **`InboundRateLimitRepository` reuse**: import directly from `services/bot_gateway/app/rate_limit_repository.py`. The repo uses `settings.inbound_rate_limit_db_path` as its DB path — since both `bot_gateway` and `user_gateway` point to the same `.data/semantaix_rate_limits.db`, rate limit is shared across channels (a sender who floods via DM to bot AND user account is rate-limited by the combined count).
  - Add `services/bot_gateway/app/` to `sys.path` in `user_gateway` — OR extract `rate_limit_repository.py` to `platform_common/` as a shared module. **Preferred**: move to `platform_common/rate_limit_repository.py` since it now has two consumers. Update import in `bot_gateway` accordingly.

### Out of Scope

- Sending any reply for rate-limited or spam-dropped messages (NFR-15-05, NFR-15-09 — always silent).
- Operator-account filter (15.03 owns it).
- Keyword hot-reload without restart — deferred; reload requires service restart for now.

## Implementation Notes

- **Shared rate limit repo**: Moving `InboundRateLimitRepository` to `platform_common/` is the correct architectural move (two consumers in separate services). The move is a refactor within this story — update `bot_gateway` import path, add a `platform_common` test to replace the `bot_gateway` repo test if the test file moves too.
- **URL regex**: `re.findall(r'https?://\S+', text or "")`. Counts raw URL tokens. No DNS lookup. Threshold is 3 (>3 = drop).
- **Keyword matching**: Use `re.search(r'\b' + re.escape(keyword) + r'\b', text, re.IGNORECASE)`. Whole-word boundary prevents false positives on substrings. For Russian text (no word boundaries in regex), use a simple `keyword in text.lower()` fallback if `\b` fails on non-ASCII — test both cases.
- **`datetime.now(UTC)` injection**: `SpamFilter.is_spam` takes a `now: datetime` parameter for deterministic tests (same pattern as `InboundRateLimitRepository.check_and_record`).
- **`sender.scam`, `sender.fake`, `sender.bot`**: Telethon `User` attributes. In tests, use `SimpleNamespace(scam=True, fake=False, bot=False, ...)` to simulate each case.
- **`message.fwd_from`**: Telethon `MessageFwdHeader`. If `fwd_from.from_id` is set (known sender, e.g., a channel), it is NOT anonymous — only drop when `fwd_from.from_name` is set (the name is only provided when the original sender is anonymous/deleted).
- **Keyword file path**: `settings.user_gateway_spam_keywords_path: str = "services/user_gateway/data/spam_keywords.txt"` — new Settings field. Add to `.env.example`: `USER_GATEWAY_SPAM_KEYWORDS_PATH=services/user_gateway/data/spam_keywords.txt`.
- **No reply, ever**: Remove `_RATE_LIMIT_REPLY` from `user_gateway` entirely. Any code path that would send a message is a bug. Add a comment in `_forward` and `_is_spam` reminding future authors of NFR-15-05.
- **Filter order matters**: Rate limit is checked after sender-type checks because we don't want to consume rate-limit budget for scam accounts (they're dropped first).

## Test Plan

### Unit

- `tests/user_gateway/test_spam_filter.py`:
  - `sender.scam=True` → `is_spam=True`, reason `"scam_fake"`.
  - `sender.fake=True` → `is_spam=True`, reason `"scam_fake"`.
  - `sender.bot=True` → `is_spam=True`, reason `"bot_sender"`.
  - `fwd_from` set with `from_name` → `is_spam=True`, reason `"anonymous_forward"`.
  - `fwd_from` set without `from_name` (known channel forward) → not spam on this check alone.
  - Rate limit returns False → `is_spam=True`, reason `"rate_limited"`.
  - 4 URLs in text → `is_spam=True`, reason `"url_flood"`.
  - 3 URLs in text → not spam on this check alone.
  - Keyword match (case-insensitive) → `is_spam=True`, reason `"spam_keyword"`.
  - No keyword match → not spam.
  - Keyword file missing → `_load_keywords` returns empty set + WARNING; no crash.
  - Clean message (no flags, under rate limit, 0 URLs, no keywords) → `(False, "")`.
  - Filter order: scam sender with 4 URLs → reason is `"scam_fake"` (first filter wins).

- `tests/user_gateway/test_message_router_with_filters.py` (extends 15.03 tests):
  - Spam message → dropped, DEBUG log with `sender_id` and `reason`, no forwarding.
  - Message exactly at length limit → forwarded as-is.
  - Message 1 char over limit → truncated to `inbound_max_message_chars`.
  - Log output never contains message text.

- `tests/platform_common/test_rate_limit_repository.py` (renamed/moved from `tests/test_bot_gateway_rate_limit_repository.py` if repo moved to platform_common):
  - All existing tests pass unchanged.

### Contract

- No reply is ever sent in any test — assert `bot.send_message` / `client.send_message` is never called in `user_gateway` code paths.

### Integration

- `tests/user_gateway/test_spam_integration.py`:
  - Full event flow with scam sender → no call to `api` (verify via `respx` no-match).
  - Rate-limited sender (6 messages in a row, limit=5) → first 5 forwarded, 6th dropped.
  - URL flood → dropped.

## Automated E2E Verification

- Integration tests with mock Telethon + `respx` cover all filter paths.

## Manual Verification

1. Send a DM from an account with `scam` flag → no response, no HITL ticket.
2. Add a keyword to `spam_keywords.txt`, restart `user_gateway`, send a message containing the keyword → no response.
3. Send 11 messages rapidly from the same account → first 10 forwarded, 11th silently dropped (verify in `api` logs: only 10 inbound requests).
