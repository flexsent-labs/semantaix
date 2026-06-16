#!/usr/bin/env bash
# Epic 10.5 signoff: operator-project model refinement.
# Demonstrates:
#   1. No hitl_primary_operator_* keys in Settings (10.5-01 migration smoke)
#   2. /hitl_config @user chat_id project_slug registers operator in named project (10.5-03)
#   3. Sticky routing: second operator for a chat receives subsequent escalations (10.5-02)
#   4. Cross-project isolation: P1 operator does not receive P2 escalations (10.5-04)
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

HITL_DB="${ROOT_DIR}/.data/epic10_5_signoff_hitl.sqlite3"
RAG_DB="${ROOT_DIR}/.data/epic10_5_signoff_rag.sqlite3"
KNOWLEDGE_DB="${ROOT_DIR}/.data/epic10_5_signoff_knowledge.sqlite3"
TRACE_DB="${ROOT_DIR}/.data/epic10_5_signoff_traces.sqlite3"
NL_DB="${ROOT_DIR}/.data/epic10_5_signoff_nl.sqlite3"
BACKUP_DB="${ROOT_DIR}/.data/epic10_5_signoff_backups.sqlite3"
PROJECTS_DB="${ROOT_DIR}/.data/epic10_5_signoff_projects.sqlite3"
OPERATORS_DB="${ROOT_DIR}/.data/epic10_5_signoff_operators.sqlite3"
DEDUP_DB="${ROOT_DIR}/.data/epic10_5_signoff_dedup.sqlite3"
INCIDENT_DB="${ROOT_DIR}/.data/epic10_5_signoff_incidents.sqlite3"
CALENDAR_DB="${ROOT_DIR}/.data/epic10_5_signoff_calendar.sqlite3"
rm -f "${HITL_DB}" "${RAG_DB}" "${KNOWLEDGE_DB}" "${TRACE_DB}" \
      "${NL_DB}" "${BACKUP_DB}" "${PROJECTS_DB}" "${OPERATORS_DB}" \
      "${DEDUP_DB}" "${INCIDENT_DB}" "${CALENDAR_DB}"

export HITL_TICKET_DB_PATH="${HITL_DB}"
export RAG_DB_PATH="${RAG_DB}"
export KNOWLEDGE_DB_PATH="${KNOWLEDGE_DB}"
export ANSWER_TRACE_DB_PATH="${TRACE_DB}"
export NL_OPS_DB_PATH="${NL_DB}"
export BACKUP_DB_PATH="${BACKUP_DB}"
export PROJECTS_DB_PATH="${PROJECTS_DB}"
export OPERATORS_DB_PATH="${OPERATORS_DB}"
export CALENDAR_DB_PATH="${CALENDAR_DB}"
export WEBHOOK_DEDUP_DB_PATH="${DEDUP_DB}"
export INCIDENT_DB_PATH="${INCIDENT_DB}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-stub-key}"
export TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-stub-token}"
export TELEGRAM_ALERT_CHAT_ID=""
export INTERNAL_SERVICE_TOKEN="epic10-5-internal-token"
export ADMIN_INTERNAL_TOKEN="epic10-5-internal-token"
export API_INTERNAL_BASE_URL="http://127.0.0.1:8000"

AUTH_HEADER="x-internal-token: ${ADMIN_INTERNAL_TOKEN}"

uvicorn services.api.app.main:app --port 8000 >/tmp/epic10_5-api.log 2>&1 &
API_PID=$!
uvicorn services.bot_gateway.app.main:app --port 8002 >/tmp/epic10_5-bot.log 2>&1 &
BOT_PID=$!
trap 'kill "${API_PID}" "${BOT_PID}" >/dev/null 2>&1 || true' EXIT

until curl -s http://127.0.0.1:8000/health/live >/dev/null 2>&1 && \
      curl -s http://127.0.0.1:8002/health/live >/dev/null 2>&1; do sleep 1; done

# ── Step 1: Verify no primary-operator keys in live settings ──────────────────
echo "== 10.5-01: verify no hitl_primary_operator_* in settings =="
python3 - <<'PY'
from platform_common.settings import AppSettings
s = AppSettings()
for field in ("hitl_primary_operator_username", "hitl_primary_operator_chat_id"):
    if hasattr(s, field):
        raise SystemExit(f"FAIL: {field} still present in AppSettings")
print({"primary_operator_fields_removed": True})
PY

# ── Step 2: Create a second project (salon) ───────────────────────────────────
echo "== 10.5-03: create project 'salon' =="
SALON_JSON=$(curl -s -X POST http://127.0.0.1:8000/projects \
  -H "${AUTH_HEADER}" -H 'content-type: application/json' \
  -d '{"slug":"salon","name":"Салон"}')
echo "${SALON_JSON}" | python3 -m json.tool
SALON_PROJECT_ID=$(echo "${SALON_JSON}" | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")

# Enable calendar on salon so the scope guard defers booking-intent inbounds to HITL
# (mirrors what CalendarSettingsRepository.enable does in the OAuth callback)
SALON_PROJECT_ID="${SALON_PROJECT_ID}" CALENDAR_DB_PATH="${CALENDAR_DB}" python3 - <<'PY'
import os
from services.api.app.calendar.settings_repository import CalendarSettingsRepository
repo = CalendarSettingsRepository(db_path=os.environ["CALENDAR_DB_PATH"])
repo.enable(int(os.environ["SALON_PROJECT_ID"]), calendar_operator="@op-salon")
print({"calendar_enabled_for_salon": True})
PY

# ── Step 3: Register @op-salon in the salon project via 4-part /hitl_config ──
echo "== 10.5-03: /hitl_config @op-salon 303 salon (4-part form) =="
curl -s -X POST http://127.0.0.1:8002/telegram/webhook \
  -H 'content-type: application/json' \
  -d "{\"update_id\":8001,\"message\":{\"message_id\":1,\"from\":{\"id\":1,\"username\":\"${TELEGRAM_ALERT_USERNAME:-ajdevy}\"},\"chat\":{\"id\":1,\"type\":\"private\"},\"text\":\"/hitl_config @op-salon 303 salon\"}}" \
  | python3 -m json.tool

# Verify the operator was registered under the salon project
echo "== 10.5-03: verify @op-salon in salon project =="
python3 - <<PY
import os, sqlite3
db = os.environ["OPERATORS_DB_PATH"]
con = sqlite3.connect(db)
con.row_factory = sqlite3.Row
row = con.execute("SELECT username, chat_id, project_id FROM operators WHERE username='@op-salon'").fetchone()
if row is None:
    raise SystemExit("FAIL: @op-salon not found in operators table")
print({"username": row["username"], "chat_id": row["chat_id"], "project_id": row["project_id"]})
PY

# ── Step 4: Register @op-default in the default project via 3-part /hitl_config
echo "== 10.5-02: /hitl_config @op-default 101 (3-part form, default project) =="
curl -s -X POST http://127.0.0.1:8002/telegram/webhook \
  -H 'content-type: application/json' \
  -d "{\"update_id\":8002,\"message\":{\"message_id\":2,\"from\":{\"id\":1,\"username\":\"${TELEGRAM_ALERT_USERNAME:-ajdevy}\"},\"chat\":{\"id\":1,\"type\":\"private\"},\"text\":\"/hitl_config @op-default 101\"}}" \
  | python3 -m json.tool

# ── Step 5: Seed a prior HITL ticket for chat 888 assigned to @op-salon ───────
echo "== 10.5-04: seed prior ticket for chat 888 → @op-salon (P2 context) =="
python3 - <<PY
import os, sqlite3
from datetime import UTC, datetime

hitl_db = os.environ["HITL_TICKET_DB_PATH"]
now = datetime.now(UTC).isoformat()
con = sqlite3.connect(hitl_db)
con.execute(
    "INSERT INTO hitl_tickets (conversation_ref, reason, status, operator_username, "
    "target_chat_id, created_at, updated_at, resolved_at) "
    "VALUES ('chat:888:prior', 'prior', 'assigned', '@op-salon', 888, ?, ?, NULL)",
    (now, now),
)
con.commit()
print({"prior_ticket_seeded": True, "operator": "@op-salon", "chat_id": 888})
PY

# ── Step 6: Send inbound from chat 888 — must escalate to @op-salon, not @op-default
echo "== 10.5-04: inbound from chat 888 → must escalate to @op-salon (cross-project isolation) =="
RESP=$(curl -s -X POST http://127.0.0.1:8000/conversations/inbound \
  -H 'content-type: application/json' \
  -d '{"text":"записаться","chat_id":888,"trace_id":"signoff-10-5-iso"}')
echo "${RESP}"
python3 - "${RESP}" <<'PY'
import json, sys
body = json.loads(sys.argv[1])
assignee = body.get("hitl_operator_username")
if assignee == "@op-default":
    raise SystemExit(f"FAIL cross-project isolation: @op-default received P2 escalation: {body}")
if not body.get("escalated") or assignee != "@op-salon":
    raise SystemExit(f"FAIL: expected @op-salon but got: {body}")
print({"isolation_ok": True, "assignee": assignee})
PY

echo "Epic 10.5 demo OK. Logs: /tmp/epic10_5-api.log /tmp/epic10_5-bot.log"
