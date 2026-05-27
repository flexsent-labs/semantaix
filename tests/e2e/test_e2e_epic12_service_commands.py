"""Epic 12 Story 12.02 — operator service catalog commands end-to-end.

Drives the bot→api round trip through ``TestClient``:

1. Operator sends ``/service_add Медовеевка Лайт | Лайт уровень, с видами``
   → api row exists, bot DMs ``Добавлено: Медовеевка Лайт (id=1)``.
2. Operator sends ``/service_list`` → bot DMs the single row.
3. Operator sends ``/service_remove 1`` → row marked inactive,
   ``/service_list`` now returns the empty-state hint.

Telegram is stubbed at the module-level ``_send_dm`` (the existing
pattern in the Epic-12 KB-auto-material e2e). Only the bot's ApiClient
HTTP calls are re-routed through the api TestClient; every endpoint
+ repository write executes for real.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.main import app as api_app
from services.api.app.sales.services_repository import ServicesRepository
from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-02"),
]

_INTERNAL_TOKEN = "e2e-internal-token"
_OPERATOR_USERNAME = "ajdevy"
_OPERATOR_AT = f"@{_OPERATOR_USERNAME}"
_PROJECT_ID = 71
_CHAT_ID = 555_002


class _StubHitlRepo:
    def get_runtime_config(self, key: str) -> str | None:
        return None

    def set_runtime_config(self, **_: Any) -> None:  # pragma: no cover
        return None

    def list_all(self) -> list:  # pragma: no cover
        return []


@pytest.fixture
def wired_stack(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    sales_db = tmp_path / "sales.sqlite3"

    # api: rebind to tmp DBs and the shared services repo points at the same file.
    monkeypatch.setattr(api_main.settings, "sales_db_path", str(sales_db))
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", _INTERNAL_TOKEN
    )
    monkeypatch.setattr(
        api_main,
        "sales_services_repository",
        ServicesRepository(db_path=str(sales_db)),
    )
    # Stub the operator registry lookup so the resolver returns _PROJECT_ID
    # without needing a real operators DB seeded.
    operator_record = {
        "username": _OPERATOR_AT,
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "is_active": True,
    }

    async def fake_find_operator_by_username(*, username: str) -> dict | None:
        if username == _OPERATOR_AT:
            return operator_record
        return None

    # bot: rewire api_client + send_dm.
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(
        bot_main.settings, "hitl_primary_operator_username", _OPERATOR_AT
    )
    monkeypatch.setattr(
        bot_main.settings, "internal_service_token", _INTERNAL_TOKEN
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_config_admin_username", "@admin-noop"
    )
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    api_tc = TestClient(api_app)

    async def fake_add_sales_service(
        *,
        project_id: int,
        name: str,
        description_md: str | None,
        tags: list[str] | None,
        internal_token: str,
    ) -> dict:
        response = api_tc.post(
            "/sales/services",
            json={
                "project_id": project_id,
                "name": name,
                "description_md": description_md,
                "tags": tags,
            },
            headers={"Authorization": f"Bearer {internal_token}"},
        )
        if response.status_code == 409:
            from services.bot_gateway.app.api_client import ApiError

            raise ApiError(
                "duplicate",
                request=response.request,
                response=response,
                detail=response.json().get("detail"),
            )
        response.raise_for_status()
        return response.json()

    async def fake_list_sales_services(
        *, project_id: int, internal_token: str
    ) -> dict:
        response = api_tc.get(
            "/sales/services",
            params={"project_id": project_id},
            headers={"Authorization": f"Bearer {internal_token}"},
        )
        response.raise_for_status()
        return response.json()

    async def fake_delete_sales_service(
        *, service_id: int, internal_token: str
    ) -> dict:
        response = api_tc.delete(
            f"/sales/services/{service_id}",
            headers={"Authorization": f"Bearer {internal_token}"},
        )
        if response.status_code == 404:
            from services.bot_gateway.app.api_client import ApiError

            raise ApiError(
                "missing",
                request=response.request,
                response=response,
                detail=response.json().get("detail"),
            )
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(
        bot_main.api_client,
        "find_operator_by_username",
        fake_find_operator_by_username,
        raising=False,
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "add_sales_service",
        fake_add_sales_service,
        raising=False,
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "list_sales_services",
        fake_list_sales_services,
        raising=False,
    )
    monkeypatch.setattr(
        bot_main.api_client,
        "delete_sales_service",
        fake_delete_sales_service,
        raising=False,
    )

    return {
        "sent_dms": sent_dms,
        "sales_db": sales_db,
    }


def _payload(*, text: str, update_id: int) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "chat": {"id": _CHAT_ID},
            "from": {"id": 200, "username": _OPERATOR_USERNAME},
            "text": text,
        },
    }


def test_service_add_list_remove_roundtrip(wired_stack) -> None:
    client = TestClient(bot_app)
    sent = wired_stack["sent_dms"]

    add_resp = client.post(
        "/telegram/webhook",
        json=_payload(
            text="/service_add Медовеевка Лайт | Лайт уровень, с видами",
            update_id=1,
        ),
    )
    assert add_resp.status_code == 200
    assert add_resp.json().get("route") == "service_add"
    assert (_CHAT_ID, "Добавлено: Медовеевка Лайт (id=1)") in sent

    list_resp = client.post(
        "/telegram/webhook", json=_payload(text="/service_list", update_id=2)
    )
    assert list_resp.status_code == 200
    assert (
        _CHAT_ID,
        "1. Медовеевка Лайт — Лайт уровень, с видами",
    ) in sent

    remove_resp = client.post(
        "/telegram/webhook",
        json=_payload(text="/service_remove 1", update_id=3),
    )
    assert remove_resp.status_code == 200
    assert (_CHAT_ID, "Удалено: id=1") in sent

    list_after = client.post(
        "/telegram/webhook", json=_payload(text="/service_list", update_id=4)
    )
    assert list_after.status_code == 200
    assert (
        _CHAT_ID,
        "Услуг пока нет. Добавьте первую через /service_add <название>.",
    ) in sent
