#!/usr/bin/env bash
# Epic 02 signoff: incident ingest -> read -> ack -> resolve -> timeline.
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

INCIDENT_DB="${ROOT_DIR}/.data/epic02_signoff_incidents.sqlite3"
HITL_DB="${ROOT_DIR}/.data/epic02_signoff_hitl.sqlite3"
RAG_DB="${ROOT_DIR}/.data/epic02_signoff_rag.sqlite3"
KNOWLEDGE_DB="${ROOT_DIR}/.data/epic02_signoff_knowledge.sqlite3"
TRACE_DB="${ROOT_DIR}/.data/epic02_signoff_traces.sqlite3"
NL_DB="${ROOT_DIR}/.data/epic02_signoff_nl.sqlite3"
BACKUP_DB="${ROOT_DIR}/.data/epic02_signoff_backups.sqlite3"
rm -f "${INCIDENT_DB}" "${HITL_DB}" "${RAG_DB}" "${KNOWLEDGE_DB}" "${TRACE_DB}" "${NL_DB}" "${BACKUP_DB}"

export INCIDENT_DB_PATH="${INCIDENT_DB}"
export HITL_TICKET_DB_PATH="${HITL_DB}"
export RAG_DB_PATH="${RAG_DB}"
export KNOWLEDGE_DB_PATH="${KNOWLEDGE_DB}"
export ANSWER_TRACE_DB_PATH="${TRACE_DB}"
export NL_OPS_DB_PATH="${NL_DB}"
export BACKUP_DB_PATH="${BACKUP_DB}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-stub-key}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-stub-token}"
export TELEGRAM_ALERT_CHAT_ID=""

uvicorn services.api.app.main:app --port 8000 >/tmp/epic02-api.log 2>&1 &
API_PID=$!
trap 'kill "${API_PID}" >/dev/null 2>&1 || true' EXIT
until curl -s http://127.0.0.1:8000/health/live >/dev/null 2>&1; do sleep 0.5; done

echo "== ingest non-critical incident =="
INGEST=$(curl -s -X POST http://127.0.0.1:8000/incidents/events \
  -H 'content-type: application/json' \
  -d '{"fingerprint":"epic02_demo","severity":"warning","summary":"Epic 02 demo incident"}')
echo "${INGEST}"
INCIDENT_ID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "${INGEST}")

echo "== mark read =="
curl -s -X POST "http://127.0.0.1:8000/incidents/${INCIDENT_ID}/read" >/dev/null

echo "== acknowledge =="
curl -s -X POST "http://127.0.0.1:8000/incidents/${INCIDENT_ID}/ack" >/dev/null

echo "== resolve =="
curl -s -X POST "http://127.0.0.1:8000/incidents/${INCIDENT_ID}/resolve" >/dev/null

echo "== timeline verification =="
TIMELINE=$(curl -s "http://127.0.0.1:8000/incidents/${INCIDENT_ID}/timeline")
echo "${TIMELINE}"
python3 - "${TIMELINE}" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
events = [event["event_type"] for event in data["events"]]
expected = {"created", "read", "acknowledged", "resolved"}
missing = expected - set(events)
if missing:
    raise SystemExit(f"Epic 02 demo failed: missing timeline events {missing}")
print({"events": events})
PY

echo "Epic 02 demo OK. Logs: /tmp/epic02-api.log"
