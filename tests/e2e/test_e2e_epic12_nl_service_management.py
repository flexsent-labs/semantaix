"""Epic 12 Story 12.02b — NL operator dialog for service management e2e.

End-to-end coverage of the LLM-classified service catalog operator flow:

1. Operator DMs ``"добавь услугу Медовеевка Лайт — лайт уровень, с видами"``
   → LLM classifies ``{action: "add", name: …, description: …}`` → bot calls
   the api ``POST /sales/services`` → row exists, operator gets
   ``Добавлено: Медовеевка Лайт (id=1)``.
2. Operator DMs ``"список услуг"`` → LLM classifies ``{action: "list"}`` →
   bot DMs the rendered list of the single row.
3. Operator DMs ``"удали услугу Медовеевка Лайт"`` → LLM classifies
   ``{action: "remove", name: …}`` → bot resolves id by name + DELETEs →
   row marked inactive.

Telegram is stubbed at ``_send_dm`` (the established Epic-12 e2e pattern).
The OpenRouter client is stubbed by replacing the module-level
``operator_service_nl_openrouter.complete_json`` so no network call escapes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
    pytest.mark.story("12-02b"),
]

_INTERNAL_TOKEN = "e2e-internal-token"
_OPERATOR_USERNAME = "ajdevy"
_OPERATOR_AT = f"@{_OPERATOR_USERNAME}"
_PROJECT_ID = 72
_CHAT_ID = 555_003


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

    monkeypatch.setattr(api_main.settings, "sales_db_path", str(sales_db))
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", _INTERNAL_TOKEN
    )
    monkeypatch.setattr(
        api_main,
        "sales_services_repository",
        ServicesRepository(db_path=str(sales_db)),
    )

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

    # Stub the LLM classifier — each call returns the canned action shape for
    # the operator phrase being tested. The bot dispatch chooses which call is
    # next based on the text it received.
    llm_responses_by_text: dict[str, dict] = {
        "добавь услугу Медовеевка Лайт — лайт уровень, с видами": {
            "action": "add",
            "name": "Медовеевка Лайт",
            "description": "лайт уровень, с видами",
        },
        "список услуг": {"action": "list", "name": None, "description": None},
        "удали услугу Медовеевка Лайт": {
            "action": "remove",
            "name": "Медовеевка Лайт",
            "description": None,
        },
    }

    async def fake_complete_json(*, system: str, user: str, model: str | None = None) -> dict:
        if user in llm_responses_by_text:
            return llm_responses_by_text[user]
        # Anything unexpected falls through (action: null) so we don't risk
        # accidentally classifying unrelated noise as a service op.
        return {"action": None}

    monkeypatch.setattr(
        bot_main.operator_service_nl_openrouter,
        "complete_json",
        AsyncMock(side_effect=fake_complete_json),
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


def test_nl_service_add_list_remove_roundtrip(wired_stack) -> None:
    client = TestClient(bot_app)
    sent = wired_stack["sent_dms"]

    add_resp = client.post(
        "/telegram/webhook",
        json=_payload(
            text="добавь услугу Медовеевка Лайт — лайт уровень, с видами",
            update_id=1,
        ),
    )
    assert add_resp.status_code == 200
    body = add_resp.json()
    assert body.get("route") == "service_add"
    assert body.get("service_id") == "1"
    assert (_CHAT_ID, "Добавлено: Медовеевка Лайт (id=1)") in sent

    list_resp = client.post(
        "/telegram/webhook", json=_payload(text="список услуг", update_id=2)
    )
    assert list_resp.status_code == 200
    assert list_resp.json().get("route") == "service_list"
    assert (
        _CHAT_ID,
        "1. Медовеевка Лайт — лайт уровень, с видами",
    ) in sent

    remove_resp = client.post(
        "/telegram/webhook",
        json=_payload(text="удали услугу Медовеевка Лайт", update_id=3),
    )
    assert remove_resp.status_code == 200
    assert remove_resp.json().get("route") == "service_remove"
    assert (_CHAT_ID, "Удалено: id=1") in sent
