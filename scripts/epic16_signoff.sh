#!/usr/bin/env bash
# Epic 16 signoff: operator self-registration and onboarding.
#
# Validates lint, 100% coverage gate, and Epic 16 e2e pytest flows:
#   register → admin approve → onboarding buttons → operator_user channel.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "== Epic 16: ruff =="
ruff check .

echo "== Epic 16: pytest coverage (100% gate) =="
pytest --cov --cov-config=.coveragerc --cov-report=term-missing

echo "== Epic 16: e2e tests =="
pytest -m e2e tests/e2e/test_e2e_epic16_*.py -v

echo "Epic 16 signoff OK."
