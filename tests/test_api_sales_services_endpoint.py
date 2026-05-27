"""Contract tests for ``/sales/services`` (Story 12.02).

Service-token-gated POST/GET/DELETE backed by
:class:`services.api.app.sales.services_repository.ServicesRepository`.
Mirrors the existing ``/sales/materials/analyze-kb-file`` auth shape — a
Bearer token in ``Authorization``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.services_repository import ServicesRepository

_TOKEN = "test-bot-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_PROJECT_ID = 41


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    repo = ServicesRepository(db_path=str(tmp_path / "sales.sqlite3"))
    monkeypatch.setattr(api_main.settings, "internal_service_token", _TOKEN)
    monkeypatch.setattr(api_main, "sales_services_repository", repo)
    client = TestClient(api_app)
    yield {"client": client, "repo": repo}


# --- auth ------------------------------------------------------------------


def test_post_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/services",
        json={"project_id": _PROJECT_ID, "name": "x"},
    )
    assert resp.status_code == 401


def test_get_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].get(
        f"/sales/services?project_id={_PROJECT_ID}"
    )
    assert resp.status_code == 401


def test_delete_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].delete("/sales/services/1")
    assert resp.status_code == 401


# --- POST ------------------------------------------------------------------


def test_post_happy_path_returns_id(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "name": "каньонинг",
            "description_md": "Каньонинг — это…",
            "tags": ["adventure"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] > 0
    rows = env["repo"].list_active(project_id=_PROJECT_ID)
    assert len(rows) == 1
    assert rows[0].name == "каньонинг"
    assert rows[0].description_md == "Каньонинг — это…"
    assert rows[0].tags == ["adventure"]


def test_post_name_only_succeeds(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "каньонинг"},
    )
    assert resp.status_code == 200
    rows = env["repo"].list_active(project_id=_PROJECT_ID)
    assert len(rows) == 1
    assert rows[0].description_md is None
    assert rows[0].tags == []


def test_post_duplicate_returns_409(env: dict[str, Any]) -> None:
    env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "каньонинг"},
    )
    resp = env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "КАНЬОНИНГ"},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "service_already_exists"


def test_post_blank_name_returns_400(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "   "},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_service_name"


# --- GET -------------------------------------------------------------------


def test_get_empty_returns_no_services(env: dict[str, Any]) -> None:
    resp = env["client"].get(
        f"/sales/services?project_id={_PROJECT_ID}", headers=_AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {"services": []}


def test_get_lists_active_rows_only(env: dict[str, Any]) -> None:
    """``GET`` returns only ``is_active=1`` rows so a soft-deleted entry
    stops appearing immediately."""
    a = env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "alpha"},
    ).json()["id"]
    env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "beta"},
    )
    env["client"].delete(
        f"/sales/services/{a}", headers=_AUTH
    )
    resp = env["client"].get(
        f"/sales/services?project_id={_PROJECT_ID}", headers=_AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["services"]) == 1
    assert body["services"][0]["name"] == "beta"
    assert body["services"][0]["is_active"] is True


def test_get_returns_full_service_shape(env: dict[str, Any]) -> None:
    env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "name": "каньонинг",
            "description_md": "описание",
            "tags": ["adventure", "outdoors"],
        },
    )
    body = env["client"].get(
        f"/sales/services?project_id={_PROJECT_ID}", headers=_AUTH
    ).json()
    row = body["services"][0]
    assert row["project_id"] == _PROJECT_ID
    assert row["name"] == "каньонинг"
    assert row["description_md"] == "описание"
    assert row["tags"] == ["adventure", "outdoors"]
    assert row["is_active"] is True


# --- DELETE ----------------------------------------------------------------


def test_delete_flips_is_active(env: dict[str, Any]) -> None:
    body = env["client"].post(
        "/sales/services",
        headers=_AUTH,
        json={"project_id": _PROJECT_ID, "name": "x"},
    ).json()
    resp = env["client"].delete(
        f"/sales/services/{body['id']}", headers=_AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert env["repo"].list_active(project_id=_PROJECT_ID) == []


def test_delete_unknown_returns_404(env: dict[str, Any]) -> None:
    resp = env["client"].delete("/sales/services/9999", headers=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "service_not_found"
