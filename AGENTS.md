# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Development Setup

Requires Python 3.11:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
```

Copy `.env.example` to `.env` and fill in required secrets (OpenRouter API key, Telegram token, operator chat IDs).

## Commands

```bash
# Lint
ruff check .

# Test with coverage (100% required on platform_common/ and services/)
pytest --cov --cov-config=.coveragerc --cov-report=term-missing

# Run a single test file
pytest tests/test_foo.py -v

# Full stack (Docker)
docker compose up --build -d

# Full Epic signoff (CI parity + live demo)
bash scripts/run_all_epic_feature_signoffs.sh
```

CI runs `ruff check .` then `pytest` with coverage on every PR and push to main.

## Architecture

**Semantaix** is a Docker-first microservices platform with five FastAPI services behind an nginx reverse proxy:

| Service | Port | Role |
|---------|------|------|
| `api` | 8000 | Core business logic (all epics) |
| `web_ui` | 8001 | Admin shell UI |
| `bot_gateway` | 8002 | Telegram webhook ingress |
| `ingest_worker` | 8003 | Heartbeat placeholder |
| `scheduler` | 8004 | Heartbeat placeholder |

**Infrastructure:** nginx (port 80) routes `/api` → api, `/admin` → web_ui, `/telegram/webhook` → bot_gateway. Qdrant (port 6333) is the vector store. SQLite databases live in `.data/`.

### Key Data Stores (SQLite)

Each concern has its own DB file in `.data/`:
- `semantaix_story1.db` — Telegram message transcripts
- `semantaix_incidents.db` — Incidents + event timeline
- `semantaix_hitl.db` — HITL tickets + runtime config
- `semantaix_rag.db` — RAG chunks (SHA-256 dedup)
- `semantaix_knowledge.db` — Knowledge candidates + moderation queue (WAL)
- `semantaix_operator_files.db` — Operator file registry (WAL; cross-read RO by api)
- `semantaix_web_auth.db` — Web UI auth: one-time login codes + permanent sessions

Both the `operator_files` and `knowledge_moderation_candidates` tables run in WAL mode so the api service can open `semantaix_operator_files.db` read-only and ATTACH the knowledge DB in a single SQLite query (see `services/api/app/operator_files_view.py`).

### Core API Flows (`services/api/`)

- **`/conversations/inbound`** — single entry point for every customer message. Builds an `AnswerContext`, runs an `AnswerPipeline` of answerers in order: `DateTimeAnswerer` → `HolidayAnswerer` (RU calendar by default via `holidays`) → `WeatherAnswerer` (Open-Meteo, with Cyrillic→Latin city map) → `GroundedRagAnswerer` (RAG retrieve → strict-grounding LLM with `ESCALATE_TO_HUMAN` sentinel → LLM verifier → regex guardrails → profanity check). If no answerer handles the question, it escalates to HITL: ack to customer + create+assign ticket + DM operator with the verbatim question. The LLM is never in the user-visible answer unless it passes all four grounding layers. Pipeline lives in `services/api/app/answerers/`.
- **`/incidents/*`** — Dedup window (300 s default), status lifecycle, event timeline in `incidents.py`
- **`/hitl/tickets/*`** — Route/assign/reply workflow in `hitl.py`; reply auto-resolves the ticket. Runtime config (operator mapping, ack message, country/timezone/location, grounding threshold) stored in `hitl_runtime_config`.
- **`/knowledge/extract`** — Pulls transcript lines → moderation candidates (`knowledge_moderation.py`)
- **`/knowledge/candidates/*`** — Approve (triggers RAG reindex) or reject via `knowledge_moderation.py`
- **`/rag/ingest`** + **`/rag/retrieve`** — Line-split ingest with dedup; lemma-overlap retrieval in `rag.py` (via `RussianNormalizer.lemmas`).
- **`/admin/auth/*`** — Telegram-code login (`admin_auth.py`). `request_code` resolves chat_id via `operator_chat_lookup.py` and DMs a 6-digit code; `verify` consumes it, rotates prior sessions, and sets an `HttpOnly; SameSite=Lax` cookie (`semantaix_session`). `me` returns the principal; `logout` revokes. Codes have 5-min TTL and a 5-attempt cap; sessions never expire.
- **`/admin/files`, `/admin/files/{short_id}`, `/admin/files/search`** — Inspect extracted text of operator uploads (`admin_files.py` + `operator_files_view.py`). Admin sees all (including confidential); operator sees own only — enforced in SQL WHERE clauses. Accepts either a cookie session OR `Authorization: Bearer <internal_service_token>` + `as_user=` for service-to-service calls from the bot.

### Shared Foundation (`platform_common/`)

- `settings.py` — Single `Settings` class (Pydantic, env-based) shared by all services
- `app_factory.py` — Creates FastAPI app with `/health/live`, `/ready`, `/startup` endpoints

### Bot Gateway (`services/bot_gateway/`)

Validates Telegram webhook payload, normalizes + persists messages, then branches by sender:
- **Customer message** → `ApiClient.forward_inbound` to api `/conversations/inbound`.
- **Operator message** (sender matches `hitl_primary_operator_username`) → extract ticket id from `reply_to_message` text or fall back to "single open assigned ticket"; route via `ApiClient.deliver_operator_reply` to `/hitl/tickets/{id}/reply` (which auto-resolves).
- **`/hitl_config @user chat_id`** admin command → upserts runtime config keys for operator routing.
- **`/files [N]`** operator command → list operator's recent uploads (metadata only).
- **`/file <short_id>`** operator-or-admin command → DM metadata + first 3072 chars of extracted text + link to `/admin/files/<short_id>`. Calls api `/admin/files/{short_id}` via `internal_service_token`.
- **`/files_find <query>`** operator-or-admin command → DM up to 10 hits with one-line snippets. Calls api `/admin/files/search`.

### Russian-first text handling (`services/api/app/russian_text/`)

`RussianNormalizer` wraps razdel tokenization + a static slang dictionary (`data/russian_slang.json`) + `pymorphy3` lemmatization. Used by `rag.py` `_tokenize` (so retrieval matches across inflection and common slang), by `guardrails.py` (hedge / policy phrase lists in `data/russian_hedges.txt` and `data/russian_policy_phrases.txt` run against normalized text), and by `GroundedRagAnswerer` for output profanity filtering (`data/russian_profanity.txt`). Add new slang pairs to the JSON file — the seam covers retrieval, intent, and guardrails together.

### Guardrails (`services/api/app/guardrails.py`)

Final regex check on LLM output inside `GroundedRagAnswerer`: 0.2 (blocked — empty, too long, hedging/uncertainty, policy violation) or 0.95 (valid). Lists are loaded from `data/russian_hedges.txt` and `data/russian_policy_phrases.txt` (Russian + English entries; tunable without code changes).

## Code Conventions

- Line length: 100 characters (`ruff`, `pyproject.toml`)
- Python 3.11 type hints throughout
- Repository classes own all DB access; no raw SQL outside `*Repository` classes
- Test files mirror source structure under `tests/`; async tests use `pytest-asyncio`
- 100% coverage enforced — add tests for every new branch
