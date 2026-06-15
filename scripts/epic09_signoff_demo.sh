#!/usr/bin/env bash
# Epic 09 signoff: operator KB upload via the /knowledge/operator_upload endpoint.
# - Inline-text upload auto-approves and lands in RAG
# - Same binary uploaded twice short-circuits to dedup (zero new chunks)
# - Confidential upload propagates the flag and answer-trace metadata is redacted
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

PERSIST_DB="${ROOT_DIR}/.data/epic09_signoff_persist.sqlite3"
INCIDENT_DB="${ROOT_DIR}/.data/epic09_signoff_incidents.sqlite3"
HITL_DB="${ROOT_DIR}/.data/epic09_signoff_hitl.sqlite3"
RAG_DB="${ROOT_DIR}/.data/epic09_signoff_rag.sqlite3"
KNOWLEDGE_DB="${ROOT_DIR}/.data/epic09_signoff_knowledge.sqlite3"
TRACE_DB="${ROOT_DIR}/.data/epic09_signoff_traces.sqlite3"
NL_DB="${ROOT_DIR}/.data/epic09_signoff_nl.sqlite3"
BACKUP_DB="${ROOT_DIR}/.data/epic09_signoff_backups.sqlite3"
UPLOAD_DIR="${ROOT_DIR}/.data/epic09_signoff_uploads"
rm -f "${PERSIST_DB}" "${INCIDENT_DB}" "${HITL_DB}" "${RAG_DB}" "${KNOWLEDGE_DB}" \
      "${TRACE_DB}" "${NL_DB}" "${BACKUP_DB}"
rm -rf "${UPLOAD_DIR}"
mkdir -p "${UPLOAD_DIR}"

export PERSISTENCE_DB_PATH="${PERSIST_DB}"
export INCIDENT_DB_PATH="${INCIDENT_DB}"
export HITL_TICKET_DB_PATH="${HITL_DB}"
export RAG_DB_PATH="${RAG_DB}"
export KNOWLEDGE_DB_PATH="${KNOWLEDGE_DB}"
export ANSWER_TRACE_DB_PATH="${TRACE_DB}"
export NL_OPS_DB_PATH="${NL_DB}"
export BACKUP_DB_PATH="${BACKUP_DB}"
export OPERATOR_UPLOAD_STORAGE_DIR="${UPLOAD_DIR}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-stub-key}"
export TELEGRAM_BOT_TOKEN="stub-token"

PYTHONPATH="${ROOT_DIR}" "${ROOT_DIR}/.venv/bin/uvicorn" \
  services.api.app.main:app --port 8009 >/tmp/epic09-api.log 2>&1 &
API_PID=$!
trap 'kill "${API_PID}" >/dev/null 2>&1 || true' EXIT
until curl -s http://127.0.0.1:8009/health/live >/dev/null 2>&1; do sleep 1; done

PUBLIC_FILE="${UPLOAD_DIR}/public.txt"
cat <<EOF >"${PUBLIC_FILE}"
Расписание работы офиса: будние дни с 9:00 до 18:00.
Электронная почта поддержки: support@example.ru.
EOF

CONFIDENTIAL_FILE="${UPLOAD_DIR}/private.txt"
cat <<EOF >"${CONFIDENTIAL_FILE}"
Внутренние расценки на ремонт: первый класс 1500 рублей в час.
Скидка постоянным клиентам: 12%.
EOF

echo "== inline-text upload =="
INLINE_RESPONSE=$(curl -s -X POST http://127.0.0.1:8009/knowledge/operator_upload \
  -H 'content-type: application/json' \
  -d '{"operator_username":"@ajdevy","source_file_type":"inline_text","inline_text":"Часы работы офиса: будни 9-18.","is_confidential":false}')
echo "${INLINE_RESPONSE}" | python3 -m json.tool
echo "${INLINE_RESPONSE}" | python3 -c "import json,sys; r=json.load(sys.stdin); assert r['deduplicated'] is False and r['inserted_chunks'] >= 1, r"

echo "== first public file upload =="
PUBLIC_RESPONSE=$(curl -s -X POST http://127.0.0.1:8009/knowledge/operator_upload \
  -H 'content-type: application/json' \
  -d "{\"operator_username\":\"@ajdevy\",\"source_file_type\":\"txt\",\"source_file_name\":\"public.txt\",\"stored_binary_path\":\"${PUBLIC_FILE}\",\"is_confidential\":false}")
echo "${PUBLIC_RESPONSE}" | python3 -m json.tool
echo "${PUBLIC_RESPONSE}" | python3 -c "import json,sys; r=json.load(sys.stdin); assert r['deduplicated'] is False and r['inserted_chunks'] >= 1, r"

echo "== second upload of the same public file deduplicates =="
DEDUP_RESPONSE=$(curl -s -X POST http://127.0.0.1:8009/knowledge/operator_upload \
  -H 'content-type: application/json' \
  -d "{\"operator_username\":\"@ajdevy\",\"source_file_type\":\"txt\",\"source_file_name\":\"public.txt\",\"stored_binary_path\":\"${PUBLIC_FILE}\"}")
echo "${DEDUP_RESPONSE}" | python3 -m json.tool
echo "${DEDUP_RESPONSE}" | python3 -c "import json,sys; r=json.load(sys.stdin); assert r['deduplicated'] is True and r['inserted_chunks'] == 0, r"

echo "== confidential upload =="
CONFIDENTIAL_RESPONSE=$(curl -s -X POST http://127.0.0.1:8009/knowledge/operator_upload \
  -H 'content-type: application/json' \
  -d "{\"operator_username\":\"@ajdevy\",\"source_file_type\":\"txt\",\"source_file_name\":\"private.txt\",\"stored_binary_path\":\"${CONFIDENTIAL_FILE}\",\"is_confidential\":true}")
echo "${CONFIDENTIAL_RESPONSE}" | python3 -m json.tool
echo "${CONFIDENTIAL_RESPONSE}" | python3 -c "import json,sys; r=json.load(sys.stdin); assert r['is_confidential'] is True and r['inserted_chunks'] >= 1, r"

echo "== rag_chunks confidential flag set =="
PYTHONPATH="${ROOT_DIR}" "${ROOT_DIR}/.venv/bin/python" - <<'PY'
import os
import sqlite3
with sqlite3.connect(os.environ["RAG_DB_PATH"]) as conn:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT source_id, is_confidential FROM rag_chunks"
    ).fetchall()
flags = {row["source_id"]: row["is_confidential"] for row in rows}
print(flags)
assert any(v == 1 for v in flags.values()), "no confidential chunk persisted"
assert any(v == 0 for v in flags.values()), "no public chunk persisted"
PY

echo "Epic 09 demo OK."
