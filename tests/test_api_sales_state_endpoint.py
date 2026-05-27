"""Contract tests for ``GET /sales/state`` (Story 12.02).

Service-token-gated read of :class:`StateRepository.list_active`. Optional
``chat_id`` filters server-side so the ``/sales_state @customer`` command
doesn't have to fetch + scan the whole project.

The payload omits anything that could leak a secret (telegram_file_id,
operator chat_id from the proposal, etc.) — only the curated state shape
is echoed back.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.intent import Intent
from services.api.app.sales.state_repository import StateRepository

_TOKEN = "test-bot-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_PROJECT_ID = 41
_NOW = datetime(2026, 5, 27, 18, 42, tzinfo=UTC)


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    repo = StateRepository(db_path=str(tmp_path / "sales.sqlite3"))
    monkeypatch.setattr(api_main.settings, "internal_service_token", _TOKEN)
    monkeypatch.setattr(api_main, "sales_state_repository", repo)
    client = TestClient(api_app)
    yield {"client": client, "repo": repo}


def test_get_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].get(f"/sales/state?project_id={_PROJECT_ID}")
    assert resp.status_code == 401


def test_get_empty_returns_empty_list(env: dict[str, Any]) -> None:
    resp = env["client"].get(
        f"/sales/state?project_id={_PROJECT_ID}", headers=_AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {"states": []}


def test_get_returns_active_states(env: dict[str, Any]) -> None:
    env["repo"].upsert(
        chat_id=12345,
        project_id=_PROJECT_ID,
        current_stage="scoping",
        collected_intent=Intent(dates="1 мая").to_dict(),
        now=_NOW,
        last_customer_msg_at=_NOW,
    )
    resp = env["client"].get(
        f"/sales/state?project_id={_PROJECT_ID}", headers=_AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["states"]) == 1
    row = body["states"][0]
    assert row["chat_id"] == 12345
    assert row["project_id"] == _PROJECT_ID
    assert row["current_stage"] == "scoping"
    assert row["collected_intent"]["dates"] == "1 мая"
    assert row["last_customer_msg_at"] == _NOW.isoformat()


def test_get_with_chat_id_filters(env: dict[str, Any]) -> None:
    env["repo"].upsert(
        chat_id=11,
        project_id=_PROJECT_ID,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    env["repo"].upsert(
        chat_id=22,
        project_id=_PROJECT_ID,
        current_stage="scoping",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    resp = env["client"].get(
        f"/sales/state?project_id={_PROJECT_ID}&chat_id=22", headers=_AUTH
    )
    body = resp.json()
    assert [r["chat_id"] for r in body["states"]] == [22]


def test_get_excludes_dormant(env: dict[str, Any]) -> None:
    env["repo"].upsert(
        chat_id=11,
        project_id=_PROJECT_ID,
        current_stage="dormant",
        collected_intent=Intent().to_dict(),
        now=_NOW,
    )
    body = env["client"].get(
        f"/sales/state?project_id={_PROJECT_ID}", headers=_AUTH
    ).json()
    assert body["states"] == []


def test_get_omits_telegram_file_id_from_last_proposal(
    env: dict[str, Any],
) -> None:
    """Defence-in-depth: even if a future proposal payload included a
    telegram_file_id or an operator chat_id, the curated response shape
    must not echo it back. The current contract publishes
    ``last_proposal`` verbatim, so this test guards against a leak via
    that payload by asserting the response keys explicitly."""
    env["repo"].upsert(
        chat_id=11,
        project_id=_PROJECT_ID,
        current_stage="proposing",
        collected_intent=Intent().to_dict(),
        last_proposal={"date": "2026-06-01"},
        now=_NOW,
    )
    body = env["client"].get(
        f"/sales/state?project_id={_PROJECT_ID}", headers=_AUTH
    ).json()
    [row] = body["states"]
    # Only the documented keys are echoed; no operator chat_id, no token.
    assert set(row.keys()) == {
        "chat_id",
        "project_id",
        "current_stage",
        "collected_intent",
        "last_proposal",
        "last_customer_msg_at",
        "last_bot_msg_at",
    }
