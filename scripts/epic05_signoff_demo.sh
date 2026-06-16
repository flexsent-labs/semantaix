#!/usr/bin/env bash
# Epic 05 signoff: RAG ingest then retrieve returns the seeded source.
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

INCIDENT_DB="${ROOT_DIR}/.data/epic05_signoff_incidents.sqlite3"
HITL_DB="${ROOT_DIR}/.data/epic05_signoff_hitl.sqlite3"
RAG_DB="${ROOT_DIR}/.data/epic05_signoff_rag.sqlite3"
KNOWLEDGE_DB="${ROOT_DIR}/.data/epic05_signoff_knowledge.sqlite3"
TRACE_DB="${ROOT_DIR}/.data/epic05_signoff_traces.sqlite3"
NL_DB="${ROOT_DIR}/.data/epic05_signoff_nl.sqlite3"
BACKUP_DB="${ROOT_DIR}/.data/epic05_signoff_backups.sqlite3"
rm -f "${INCIDENT_DB}" "${HITL_DB}" "${RAG_DB}" "${KNOWLEDGE_DB}" "${TRACE_DB}" "${NL_DB}" "${BACKUP_DB}"

export INCIDENT_DB_PATH="${INCIDENT_DB}"
export HITL_TICKET_DB_PATH="${HITL_DB}"
export RAG_DB_PATH="${RAG_DB}"
export KNOWLEDGE_DB_PATH="${KNOWLEDGE_DB}"
export ANSWER_TRACE_DB_PATH="${TRACE_DB}"
export NL_OPS_DB_PATH="${NL_DB}"
export BACKUP_DB_PATH="${BACKUP_DB}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-stub-key}"
export TELEGRAM_BOT_TOKEN="stub-token"

uvicorn services.api.app.main:app --port 8000 >/tmp/epic05-api.log 2>&1 &
API_PID=$!
trap 'kill "${API_PID}" >/dev/null 2>&1 || true' EXIT
until curl -s http://127.0.0.1:8000/health/live >/dev/null 2>&1; do sleep 0.5; done

echo "== ingest RAG content =="
curl -s -X POST http://127.0.0.1:8000/rag/ingest \
  -H 'content-type: application/json' \
  -d '{"source_id":"faq-billing","text":"Invoices generate on day one for the billing cycle.\nReset password using the email link."}' \
  | python3 -m json.tool

echo "== retrieve =="
RETRIEVE=$(curl -s -X POST http://127.0.0.1:8000/rag/retrieve \
  -H 'content-type: application/json' \
  -d '{"query":"reset password email","limit":3}')
echo "${RETRIEVE}" | python3 -m json.tool

python3 - "${RETRIEVE}" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
sources = [item["source_id"] for item in data["items"]]
if "faq-billing" not in sources:
    raise SystemExit(f"Epic 05 demo failed: faq-billing not in {sources}")
print({"sources": sources})
PY

echo "Epic 05 demo OK."
