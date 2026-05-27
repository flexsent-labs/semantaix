"""Contract tests for ``/sales/materials`` (Story 12.05).

Service-token gated CRUD round-trip backed by
:class:`services.api.app.sales.client_materials_repository.ClientMaterialsRepository`.
``POST`` registers from operator-uploaded files, ``GET`` lists actives by
project, ``DELETE`` flips ``is_active``. Caption is capped at 200 chars
(per the Story 12.05 epic copy rule); over-cap requests get a 400.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.client_materials_repository import (
    ClientMaterialsRepository,
)

_TOKEN = "test-bot-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_PROJECT_ID = 73


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    repo = ClientMaterialsRepository(
        db_path=str(tmp_path / "sales.sqlite3")
    )
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", _TOKEN
    )
    monkeypatch.setattr(api_main, "client_materials_repository", repo)
    client = TestClient(api_app)
    yield {"client": client, "repo": repo}


def test_post_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/materials",
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/x.mp4",
            "byte_size": 10,
        },
    )
    assert resp.status_code == 401


def test_get_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].get(
        f"/sales/materials?project_id={_PROJECT_ID}"
    )
    assert resp.status_code == 401


def test_delete_requires_bearer(env: dict[str, Any]) -> None:
    resp = env["client"].delete("/sales/materials/1")
    assert resp.status_code == 401


def test_post_round_trip_with_all_optional_fields(
    env: dict[str, Any],
) -> None:
    resp = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/data/sales/x.mp4",
            "byte_size": 4096,
            "duration_seconds": 12,
            "caption": "Гора Ачишхо",
            "tags": ["tour_preview", "summer"],
            "telegram_file_id": "TG-123",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] > 0
    rows = env["repo"].list_active(project_id=_PROJECT_ID)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "video"
    assert row.local_path == "/data/sales/x.mp4"
    assert row.byte_size == 4096
    assert row.duration_seconds == 12
    assert row.caption == "Гора Ачишхо"
    assert row.tags == ["tour_preview", "summer"]
    assert row.telegram_file_id == "TG-123"


def test_post_minimum_required_fields(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "photo",
            "local_path": "/data/sales/y.jpg",
            "byte_size": 2048,
        },
    )
    assert resp.status_code == 200, resp.text
    rows = env["repo"].list_active(project_id=_PROJECT_ID)
    assert len(rows) == 1
    assert rows[0].caption is None
    assert rows[0].telegram_file_id is None
    assert rows[0].tags == []


def test_post_caption_over_200_chars_returns_400(
    env: dict[str, Any],
) -> None:
    over_cap = "Я" * 201
    resp = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/x.mp4",
            "byte_size": 10,
            "caption": over_cap,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "caption_too_long"
    assert env["repo"].list_active(project_id=_PROJECT_ID) == []


def test_post_caption_at_200_chars_accepted(env: dict[str, Any]) -> None:
    exactly_cap = "Я" * 200
    resp = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/x.mp4",
            "byte_size": 10,
            "caption": exactly_cap,
        },
    )
    assert resp.status_code == 200, resp.text


def test_get_lists_active_rows_for_project(env: dict[str, Any]) -> None:
    a = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/a.mp4",
            "byte_size": 10,
            "tags": ["tour_preview"],
        },
    ).json()["id"]
    env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "photo",
            "local_path": "/b.jpg",
            "byte_size": 20,
        },
    )
    env["client"].delete(f"/sales/materials/{a}", headers=_AUTH)
    resp = env["client"].get(
        f"/sales/materials?project_id={_PROJECT_ID}", headers=_AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["materials"]) == 1
    assert body["materials"][0]["kind"] == "photo"
    assert body["materials"][0]["is_active"] is True


def test_get_returns_full_material_shape(env: dict[str, Any]) -> None:
    env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/x.mp4",
            "byte_size": 99,
            "caption": "cap",
            "tags": ["tour_preview"],
            "telegram_file_id": "T1",
            "duration_seconds": 5,
        },
    )
    body = env["client"].get(
        f"/sales/materials?project_id={_PROJECT_ID}", headers=_AUTH
    ).json()
    row = body["materials"][0]
    assert row["project_id"] == _PROJECT_ID
    assert row["kind"] == "video"
    assert row["local_path"] == "/x.mp4"
    assert row["byte_size"] == 99
    assert row["caption"] == "cap"
    assert row["tags"] == ["tour_preview"]
    assert row["telegram_file_id"] == "T1"
    assert row["duration_seconds"] == 5
    assert row["is_active"] is True


def test_delete_flips_is_active(env: dict[str, Any]) -> None:
    body = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "video",
            "local_path": "/x.mp4",
            "byte_size": 10,
        },
    ).json()
    resp = env["client"].delete(
        f"/sales/materials/{body['id']}", headers=_AUTH
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert env["repo"].list_active(project_id=_PROJECT_ID) == []


def test_delete_unknown_returns_404(env: dict[str, Any]) -> None:
    resp = env["client"].delete("/sales/materials/9999", headers=_AUTH)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "material_not_found"


def test_post_rejects_invalid_kind(env: dict[str, Any]) -> None:
    resp = env["client"].post(
        "/sales/materials",
        headers=_AUTH,
        json={
            "project_id": _PROJECT_ID,
            "kind": "audio",
            "local_path": "/x.mp3",
            "byte_size": 10,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_kind"
