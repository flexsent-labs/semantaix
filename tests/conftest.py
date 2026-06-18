import os
import sys
from pathlib import Path

import pytest

# Tests run with a single inbound-forward attempt — no real backoff sleeps.
# Set before any service module constructs its Settings singleton. Production
# uses the `pending_forward_retry_delays_seconds` default (2,5,10); the retry
# tests opt back in by monkeypatching the setting explicitly.
os.environ["PENDING_FORWARD_RETRY_DELAYS_SECONDS"] = ""

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_webhook_update_claims(tmp_path, monkeypatch):
    """Story 12.31 — give every test its own webhook-entry dedup store.

    The webhook claims each Telegram ``update_id`` via the module-level
    ``webhook_update_claim_repository`` singleton (bound to its DB path at
    import). The operator-command test helpers reuse fixed ``update_id`` values
    (mostly ``1``) as throwaway placeholders, so without per-test isolation the
    claim would dedup across unrelated tests — and writing to the default
    ``.data/`` file would make the suite non-hermetic across runs. Point the
    singleton at a fresh per-test SQLite file (mirrors how the api tests reset
    ``answer_trace_repository.db_path``). No-op when the bot_gateway is not
    imported (api / platform_common-only test runs).

    Story 12.103 — also isolate the per-customer inbound rate-limit store so
    tests that fire many messages to the same chat_id don't exhaust the budget
    seen by later tests."""
    module = sys.modules.get("services.bot_gateway.app.main")
    if module is not None:
        monkeypatch.setattr(
            module.webhook_update_claim_repository,
            "db_path",
            str(tmp_path / "webhook_dedup.sqlite3"),
        )
        monkeypatch.setattr(
            module.rate_limit_repo,
            "db_path",
            str(tmp_path / "rate_limit.sqlite3"),
        )
    yield


@pytest.fixture(autouse=True)
def _detach_default_platform_admin_from_operator_role(monkeypatch):
    """Keep @ajdevy usable as a test operator unless a test opts into admin.

    Production defaults ``admin_telegram_username`` / ``hitl_config_admin_username``
    to @ajdevy. The bot's operator resolver treats those usernames as non-operators,
    which breaks the large share of bot_gateway tests that stub @ajdevy as the
    registered operator. Point the platform-admin seam at a throwaway username for
    every test run; platform-admin-specific tests override this explicitly."""
    module = sys.modules.get("services.bot_gateway.app.main")
    if module is not None:
        monkeypatch.setattr(
            module.settings, "admin_telegram_username", "@test-platform-admin"
        )
        monkeypatch.setattr(
            module.settings, "hitl_config_admin_username", "@test-platform-admin"
        )
    yield


def wire_isolated_primary_operator(tmp_path, *, username: str = "@ajdevy", chat_id: int = 999001):
    """Point operators + projects DBs at tmp_path with a single active operator.

    HITL escalation tests expect ``_effective_hitl_operator_username()`` to
    resolve to ``@ajdevy``; without isolation the shared ``.data/`` operators
    table (e.g. ``@flexsentlabs`` first) breaks those assertions.
    """
    from services.api.app.main import operator_repository, project_repository

    operator_db = str(tmp_path / "operators.sqlite3")
    projects_db = str(tmp_path / "projects.sqlite3")
    operator_repository.db_path = operator_db
    operator_repository.init_schema()
    project_repository.db_path = projects_db
    project_repository.init_schema()
    default_project = project_repository.ensure_default_project()
    operator_repository.create(
        username=username,
        project_id=default_project.id,
        chat_id=chat_id,
    )
