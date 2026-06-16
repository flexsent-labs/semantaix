#!/usr/bin/env bash
# Epic feature signoffs for this repo: CI parity + per-epic live demos.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

VENV_PYTEST="${ROOT_DIR}/.venv/bin/pytest"
VENV_RUFF="${ROOT_DIR}/.venv/bin/ruff"

if [[ ! -x "${VENV_PYTEST}" ]] || [[ ! -x "${VENV_RUFF}" ]]; then
  echo "Missing .venv with dev deps. Run:" >&2
  echo "  python3.11 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt" >&2
  exit 127
fi

echo "== ruff (CI parity) =="
"${VENV_RUFF}" check .

echo "== pytest + coverage (CI parity) =="
"${VENV_PYTEST}" --cov --cov-config=.coveragerc --cov-report=term-missing

_drain_ports() {
  pkill -f "uvicorn services" 2>/dev/null || true
  while lsof -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 \
     || lsof -iTCP:8002 -sTCP:LISTEN >/dev/null 2>&1; do sleep 0.5; done
}

for epic in 01 02 05 06 07 09; do
  echo "== Epic ${epic} live demo =="
  _drain_ports
  bash "${ROOT_DIR}/scripts/epic${epic}_signoff_demo.sh"
done
echo "== Epic 03 live demo (SKIPPED: legacy /suggest endpoint removed) =="
echo "== Epic 04 live demo (SKIPPED: legacy /suggest endpoint removed) =="
echo "== Epic 08 live demo (SKIPPED: legacy /suggest endpoint removed) =="

echo "== Epic 10.5 live demo =="
_drain_ports
bash "${ROOT_DIR}/scripts/epic10_5_signoff.sh"

echo "== Epic 11 live demo =="
bash "${ROOT_DIR}/scripts/epic11_signoff.sh"

echo "All epic feature signoffs completed OK."
