# Investigation: Telegram bot is not answering

## Hand-off Brief

1. **What happened.** Telegram delivered the user's message to bot_gateway, but the gateway could not resolve the Docker-only API hostname while running locally.
2. **Where the case stands.** Concluded; local routing now uses localhost and Docker Compose explicitly keeps the `api` service hostname.
3. **What's needed next.** Keep the current local processes running and send another message to confirm the normal live path; no tunnel replacement is required.

## Case Info

| Field | Value |
| --- | --- |
| Ticket | N/A |
| Date opened | 2026-08-06 |
| Status | Concluded |
| System | macOS, Python 3.11, local FastAPI API + Telegram bot gateway + ngrok |
| Evidence sources | User report, local processes, Telegram webhook, runtime logs, source code |

## Problem Statement

The user reports that the Telegram bot is not answering messages.

## Evidence Inventory

| Source | Status | Notes |
| --- | --- | --- |
| User report | Available | Bot does not answer; exact message and timestamp are not yet provided. |
| Local API process | Available | Recently restarted on port 8000. |
| Local bot gateway | Available | Recently restarted on port 8002. |
| ngrok tunnel | Available | Existing tunnel is expected to remain unchanged. |
| Telegram webhook delivery | Available | Current webhook is the existing ngrok URL with zero pending updates and no Telegram error. |
| Runtime logs | Available | A fresh inbound update was correlated through gateway, API, and Telegram send logs. |

## Investigation Backlog

| # | Path to Explore | Priority | Status | Notes |
| - | --- | --- | --- | --- |
| 1 | Verify processes and health endpoints | High | Done | API, bot_gateway, and ngrok health checks pass. |
| 2 | Inspect Telegram webhook and ngrok ingress | High | Done | Telegram points to the existing ngrok webhook with no pending updates or errors. |
| 3 | Trace bot gateway forwarding to API | High | Done | Local gateway was using unresolved `http://api:8000`. |
| 4 | Verify API answer and outbound Telegram delivery | High | Done | The affected `привет` update reached API and produced a successful Telegram send. |
| 5 | Add regression coverage and deploy the minimal fix | High | Done | Local default and Compose override are covered by config validation and tests. |

## Timeline of Events

| Time | Event | Source | Confidence |
| --- | --- | --- | --- |
| 2026-08-06 | User reports that the bot does not answer. | User report | Confirmed |
| 2026-08-06 15:05:20 | Telegram delivered update `726743233` to bot_gateway; forwarding failed with `[Errno 8] nodename nor servname provided`. | bot_gateway runtime log | Confirmed |
| 2026-08-06 16:09:13 | After switching local API routing to localhost, the buffered `привет` update reached `/conversations/inbound` with HTTP 200. | bot_gateway/API runtime logs | Confirmed |
| 2026-08-06 16:09:18 | API sent the reply through Telegram Bot API with HTTP 200. | API runtime log | Confirmed |

## Confirmed Findings

At initial capture, no runtime failure had been confirmed yet; the follow-up
below records the live correlation.

### Finding 2: Local bot_gateway used an unresolvable Docker hostname

**Evidence:** At `2026-08-06 15:05:20`, bot_gateway logged
`telegram_update_received` for update `726743233`, then persisted it and
logged `inbound_forward_failed` with `[Errno 8] nodename nor servname provided,
or not known`. The API received no matching request until the gateway was
restarted with `API_INTERNAL_BASE_URL=http://127.0.0.1:8000`.

**Detail:** `platform_common/settings.py:85` had `http://api:8000` as the
local default. That hostname exists inside Docker Compose but not when
uvicorn services run directly on macOS.

### Finding 3: Telegram ingress and outbound delivery were healthy

**Evidence:** Telegram `getWebhookInfo` returned
`https://lustiness-apron-unmade.ngrok-free.dev/telegram/webhook`, zero pending
updates, and no last error. The affected update was accepted by the webhook,
then after the routing fix API logged `POST /conversations/inbound` 200 and
`sendMessage` 200.

**Detail:** The tunnel was not the failing boundary.

## Deduced Conclusions

### Deduction 1: The missing answer was caused before the API answer pipeline

**Based on:** Findings 2 and 3.

**Reasoning:** Telegram delivered and bot_gateway persisted the update, but
forwarding failed at DNS resolution. Once the gateway used localhost, the same
buffered update reached the API and the outbound Telegram send succeeded.

**Conclusion:** The user's symptom was caused by local service-address
configuration, not by RAG, the answer pipeline, or ngrok.

## Hypothesized Paths

### Hypothesis 1: The message is not reaching the active bot gateway

**Status:** Confirmed

**Theory:** Telegram may still have a stale webhook, or ngrok may point at a different local process.

**Supporting indicators:** The gateway log contains the DNS resolution error;
the configured default is the Docker service hostname.

**Would confirm:** Current `getWebhookInfo` plus a matching bot gateway access log for a fresh Telegram update.

**Would refute:** A bot gateway inbound log for the same update without a response.

**Resolution:** Confirmed by replaying the buffered update after setting the
local API URL to `http://127.0.0.1:8000`; API and Telegram both returned 200.

## Missing Evidence

| Gap | Impact | How to Obtain |
| --- | --- | --- |
| Fresh interactive test after the permanent config restart | Would provide an additional user-visible confirmation | Send one message to `@semantaix_bot`. |

## Source Code Trace

| Element | Detail |
| --- | --- |
| Error origin | `services/bot_gateway/app/api_client.py:69`, called by `services/bot_gateway/app/main.py:2110`. |
| Trigger | Customer message sent to `@semantaix_bot`. |
| Condition | Local `api_internal_base_url` resolved to Docker-only `api:8000`. |
| Related files | `platform_common/settings.py`, `.env.example`, `docker-compose.yml`, `services/bot_gateway/app/main.py`, ngrok webhook configuration. |

## Conclusion

**Confidence:** High

The root cause is confirmed: local bot_gateway used the Docker-only hostname
`api:8000`, so inbound Telegram messages were persisted but could not reach the
API. Local defaults now use `127.0.0.1:8000`; Docker Compose explicitly
overrides the same setting to `api:8000`.

## Recommended Next Steps

### Fix direction

Use separate service addresses for the two runtimes: localhost for direct
uvicorn processes and the Compose service name inside Docker.

### Diagnostic

Keep the existing ngrok tunnel, restart bot_gateway from the updated working
tree, and confirm one fresh message reaches API and returns through Telegram.

## Reproduction Plan

Send one short Telegram message to `@semantaix_bot`; expect a bot_gateway
inbound log, an API `/conversations/inbound` 200, and a Telegram `sendMessage`
200.

## Side Findings

- None yet.

## Follow-up: 2026-08-06 #2

### New Evidence

- The active Telegram webhook is
  `https://lustiness-apron-unmade.ngrok-free.dev/telegram/webhook`; Telegram
  reports `pending_update_count: 0` and no last error.
- API, bot_gateway, and ngrok health checks all return HTTP 200.
- Restarting bot_gateway with `API_INTERNAL_BASE_URL=http://127.0.0.1:8000`
  replayed the buffered `привет` update. API logged the inbound request and
  Telegram Bot API returned HTTP 200 for `sendMessage`.
- After adding the local value to `.env`, a plain uvicorn restart resolved
  `AppSettings().api_internal_base_url` to `http://127.0.0.1:8000`.

### Additional Findings

#### Finding 4: Runtime addresses now match the execution environment

**Evidence:** `platform_common/settings.py:82-85` documents the localhost
default; `.env.example:67-69` documents it for local runs; and
`docker-compose.yml:59-60,74-75,97-98,129-130` explicitly keeps
`http://api:8000` for Compose services.

**Detail:** This prevents a direct local restart from regressing while
preserving container-to-container routing.

### Updated Hypotheses

#### Hypothesis 1: The message is not reaching the active bot gateway

**Status:** Refuted

**Resolution:** Telegram delivered the update to the active gateway; the
failure occurred on the gateway-to-API DNS lookup.

### Backlog Changes

- Configuration fix implemented and verified: local API default plus Docker
  Compose overrides.
- Focused regression tests: `49 passed`.
- Ruff and `docker compose config --quiet`: passed.

### Updated Conclusion

**Confidence:** High

The bot was silent because the local gateway attempted to call the Docker-only
hostname `api`. The same real buffered Telegram message now reaches the API and
produces a successful outbound Telegram response. The existing ngrok tunnel
was preserved.
