#!/usr/bin/env bash
# Epic 07 signoff: seed source DB files, run backup, list, restore round-trip.
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

DEMO_ROOT="${ROOT_DIR}/.data/epic07_signoff"
rm -rf "${DEMO_ROOT}"
mkdir -p "${DEMO_ROOT}/sources" "${DEMO_ROOT}/archives"

INCIDENT_DB="${DEMO_ROOT}/incidents.sqlite3"
HITL_DB="${DEMO_ROOT}/hitl.sqlite3"
RAG_DB="${DEMO_ROOT}/rag.sqlite3"
KNOWLEDGE_DB="${DEMO_ROOT}/knowledge.sqlite3"
TRACE_DB="${DEMO_ROOT}/traces.sqlite3"
NL_DB="${DEMO_ROOT}/nl.sqlite3"
BACKUP_DB="${DEMO_ROOT}/backups.sqlite3"

SOURCE_A="${DEMO_ROOT}/sources/rag.db"
SOURCE_B="${DEMO_ROOT}/sources/knowledge.db"
echo "rag-bytes" >"${SOURCE_A}"
echo "knowledge-bytes" >"${SOURCE_B}"

export INCIDENT_DB_PATH="${INCIDENT_DB}"
export HITL_TICKET_DB_PATH="${HITL_DB}"
export RAG_DB_PATH="${RAG_DB}"
export KNOWLEDGE_DB_PATH="${KNOWLEDGE_DB}"
export ANSWER_TRACE_DB_PATH="${TRACE_DB}"
export NL_OPS_DB_PATH="${NL_DB}"
export BACKUP_DB_PATH="${BACKUP_DB}"
export BACKUP_ARCHIVE_DIR="${DEMO_ROOT}/archives"
export BACKUP_SOURCE_PATHS="${SOURCE_A},${SOURCE_B}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-stub-key}"
export TELEGRAM_BOT_TOKEN="stub-token"

uvicorn services.api.app.main:app --port 8000 >/tmp/epic07-api.log 2>&1 &
API_PID=$!
trap 'kill "${API_PID}" >/dev/null 2>&1 || true' EXIT
until curl -s http://127.0.0.1:8000/health/live >/dev/null 2>&1; do sleep 1; done

echo "== run backup =="
RUN=$(curl -s -X POST http://127.0.0.1:8000/backups/run)
echo "${RUN}" | python3 -m json.tool
BACKUP_ID=$(python3 -c "import json,sys; print(json.loads(sys.argv[1])['id'])" "${RUN}")

echo "== last successful =="
curl -s http://127.0.0.1:8000/backups/last-successful | python3 -m json.tool

echo "== restore =="
RESTORE_DIR="${DEMO_ROOT}/restored"
mkdir -p "${RESTORE_DIR}"
RESTORE=$(curl -s -X POST "http://127.0.0.1:8000/backups/${BACKUP_ID}/restore" \
  -H 'content-type: application/json' \
  -d "{\"confirm_token\":\"restore-${BACKUP_ID}\",\"target_root\":\"${RESTORE_DIR}\"}")
echo "${RESTORE}" | python3 -m json.tool

python3 - "${RESTORE_DIR}" "${SOURCE_A}" "${SOURCE_B}" <<'PY'
import filecmp, os, sys
restore_dir, src_a, src_b = sys.argv[1:4]
for src in (src_a, src_b):
    name = os.path.basename(src)
    target = os.path.join(restore_dir, name)
    if not filecmp.cmp(src, target, shallow=False):
        raise SystemExit(f"Epic 07 demo failed: byte mismatch for {name}")
print({"restored": sorted(os.listdir(restore_dir))})
PY

echo "Epic 07 demo OK."
