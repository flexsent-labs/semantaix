#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_PATH="${ROOT_DIR}/.data/epic01_signoff.sqlite3"
DEDUP_DB="${ROOT_DIR}/.data/epic01_signoff_dedup.sqlite3"

cd "${ROOT_DIR}"
mkdir -p .data
rm -f "${DB_PATH}" "${DEDUP_DB}"

# Load local environment (including OPENROUTER_API_KEY) when available.
if [[ -f "${ROOT_DIR}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${ROOT_DIR}/.env"
  set +a
fi

export PERSISTENCE_DB_PATH="${DB_PATH}"
export WEBHOOK_DEDUP_DB_PATH="${DEDUP_DB}"
export OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-demo-key-not-used}"

uvicorn services.bot_gateway.app.main:app --port 8002 >/tmp/epic01-bot.log 2>&1 &
BOT_PID=$!
uvicorn services.api.app.main:app --port 8000 >/tmp/epic01-api.log 2>&1 &
API_PID=$!

cleanup() {
  kill "${BOT_PID}" "${API_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

until curl -s http://127.0.0.1:8000/health/live >/dev/null 2>&1 && \
      curl -s http://127.0.0.1:8002/health/live >/dev/null 2>&1; do sleep 0.5; done

echo "== webhook call =="
curl -s -X POST http://127.0.0.1:8002/telegram/webhook \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/telegram/update_message_text_basic.json
echo

echo "== api health check =="
curl -s http://127.0.0.1:8000/health/live
echo

echo "== sqlite verification =="
python3 - <<'PY'
import os
import sqlite3

db_path = os.environ["PERSISTENCE_DB_PATH"]
con = sqlite3.connect(db_path)
conversation_count = con.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
message_count = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
print({"conversations": conversation_count, "messages": message_count})
PY

echo "Demo completed. Logs: /tmp/epic01-bot.log /tmp/epic01-api.log"
