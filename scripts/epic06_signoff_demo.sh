#!/usr/bin/env bash
# Epic 06 signoff: seed transcript, extract candidates, approve, re-ingest, retrieve.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
mkdir -p .data

if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

PERSIST_DB="${ROOT_DIR}/.data/epic06_signoff_persist.sqlite3"
INCIDENT_DB="${ROOT_DIR}/.data/epic06_signoff_incidents.sqlite3"
HITL_DB="${ROOT_DIR}/.data/epic06_signoff_hitl.sqlite3"
RAG_DB="${ROOT_DIR}/.data/epic06_signoff_rag.sqlite3"
KNOWLEDGE_DB="${ROOT_DIR}/.data/epic06_signoff_knowledge.sqlite3"
TRACE_DB="${ROOT_DIR}/.data/epic06_signoff_traces.sqlite3"
NL_DB="${ROOT_DIR}/.data/epic06_signoff_nl.sqlite3"
BACKUP_DB="${ROOT_DIR}/.data/epic06_signoff_backups.sqlite3"
rm -f "${PERSIST_DB}" "${INCIDENT_DB}" "${HITL_DB}" "${RAG_DB}" "${KNOWLEDGE_DB}" \
      "${TRACE_DB}" "${NL_DB}" "${BACKUP_DB}"

export PERSISTENCE_DB_PATH="${PERSIST_DB}"
export INCIDENT_DB_PATH="${INCIDENT_DB}"
export HITL_TICKET_DB_PATH="${HITL_DB}"
export RAG_DB_PATH="${RAG_DB}"
export KNOWLEDGE_DB_PATH="${KNOWLEDGE_DB}"
export ANSWER_TRACE_DB_PATH="${TRACE_DB}"
export NL_OPS_DB_PATH="${NL_DB}"
export BACKUP_DB_PATH="${BACKUP_DB}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-stub-key}"
export TELEGRAM_BOT_TOKEN="stub-token"

echo "== seed transcript =="
PYTHONPATH="${ROOT_DIR}" "${ROOT_DIR}/.venv/bin/python" - <<'PY'
import os
from services.bot_gateway.app.persistence import TelegramConversationRepository

repo = TelegramConversationRepository(os.environ["PERSISTENCE_DB_PATH"])
conversation_id = repo.create_or_get_conversation(telegram_user_id=9001)
repo.append_message_if_new(
    conversation_id=conversation_id,
    source_message_id=1,
    role="user",
    text="To reset password, click the email link from settings.",
    trace_id="seed-1",
)
repo.append_message_if_new(
    conversation_id=conversation_id,
    source_message_id=2,
    role="user",
    text="Billing invoices arrive on day one of every cycle.",
    trace_id="seed-2",
)
print({"conversation_id": conversation_id})
PY

uvicorn services.api.app.main:app --port 8000 >/tmp/epic06-api.log 2>&1 &
API_PID=$!
trap 'kill "${API_PID}" >/dev/null 2>&1 || true' EXIT
until curl -s http://127.0.0.1:8000/health/live >/dev/null 2>&1; do sleep 0.5; done

echo "== extract candidates =="
EXTRACT=$(curl -s -X POST http://127.0.0.1:8000/knowledge/extract \
  -H 'content-type: application/json' -d '{}')
echo "${EXTRACT}" | python3 -m json.tool

CANDIDATE_ID=$(python3 -c "import json,sys; ids=json.loads(sys.argv[1]).get('moderation_queue_ids', []); print(ids[0] if ids else '')" "${EXTRACT}")
if [[ -z "${CANDIDATE_ID}" ]]; then
  echo "Epic 06 demo failed: no moderation candidates created" >&2
  exit 1
fi

echo "== approve candidate ${CANDIDATE_ID} =="
APPROVE=$(curl -s -X POST "http://127.0.0.1:8000/knowledge/candidates/${CANDIDATE_ID}/approve" \
  -H 'content-type: application/json' -d '{"edited_text": null}')
echo "${APPROVE}" | python3 -m json.tool

echo "== retrieval picks up new content =="
RETRIEVE=$(curl -s -X POST http://127.0.0.1:8000/rag/retrieve \
  -H 'content-type: application/json' \
  -d '{"query":"reset password email link settings","limit":5}')
python3 - "${RETRIEVE}" "${CANDIDATE_ID}" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
expected = f"knowledge_candidate:{sys.argv[2]}"
sources = [item["source_id"] for item in data["items"]]
if expected not in sources:
    raise SystemExit(f"Epic 06 demo failed: {expected} missing from {sources}")
print({"sources": sources})
PY

echo "Epic 06 demo OK."
