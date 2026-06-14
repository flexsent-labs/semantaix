"""Unit tests for the calendar disable + service config commands
(Epic 11, story 11.08; PR #75 follow-up — /connect_calendar IS the enable
action, so the /calendar_on operator command and the admin /calendar_on @slug
command are removed).

Operator path: `/calendar_off`, `/calendar_service add|remove`.
Admin path: `/calendar_off @slug` via the admin dispatcher.
Drives the bot_gateway webhook end-to-end with stubbed `ApiClient` methods and a
fake `_send_dm`, mirroring the existing calendar-command tests.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.calendar_commands import parse_service_add
from services.bot_gateway.app.main import app as bot_app

_OPERATOR = "@calendar_op"
_PROJECT_ID = 11
_ADMIN = "@admin"


class _StubHitlRepo:
    def get_runtime_config(self, key: str):
        return None

    def set_runtime_config(self, **kwargs):
        pass

    def list_all(self):
        return []


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")
    monkeypatch.setattr(bot_main.settings, "internal_service_token", "svc-token")
    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", _ADMIN)
    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return {"tmp_path": tmp_path, "dms": sent_dms}


def _message(*, text: str, username: str = "calendar_op", chat_id: int = 100):
    return {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": chat_id},
            "from": {"id": 200, "username": username},
            "text": text,
        },
    }


def _stub_operator_lookup(monkeypatch, *, record):
    async def fake_lookup(*, username: str):
        return record

    monkeypatch.setattr(bot_main.api_client, "find_operator_by_username", fake_lookup)


def _registered_operator_record():
    return {
        "username": _OPERATOR,
        "chat_id": 100,
        "project_id": _PROJECT_ID,
        "is_active": True,
    }


# --- operator /calendar_off -------------------------------------------------


def test_operator_calendar_off_disables(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_disable(*, project_id, actor, actor_role, internal_token):
        return {"enabled": False}

    monkeypatch.setattr(bot_main.api_client, "calendar_disable", fake_disable)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/calendar_off"))
    body = response.json()
    assert body["decision"] == "disabled"
    assert "выключен" in isolated_bot["dms"][0][1]


def test_operator_calendar_off_api_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_disable(*, project_id, actor, actor_role, internal_token):
        raise httpx.RequestError("boom")

    monkeypatch.setattr(bot_main.api_client, "calendar_disable", fake_disable)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/calendar_off"))
    assert response.json()["decision"] == "api_error"


def test_calendar_off_non_operator_ignored(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=None)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_off", username="random_user"),
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unauthorized_calendar"
    assert len(isolated_bot["dms"]) == 0


# --- operator /calendar_service --------------------------------------------


def test_operator_service_add(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_upsert(**kwargs):
        captured.append(kwargs)
        return {"id": 5}

    monkeypatch.setattr(bot_main.api_client, "calendar_upsert_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_service add маникюр 60 mon-sat 10:00-19:00"),
    )
    body = response.json()
    assert body["route"] == "calendar_service"
    assert body["decision"] == "added"
    assert captured[0]["name"] == "маникюр"
    assert captured[0]["duration_minutes"] == 60
    assert captured[0]["service_days"] == ["mon", "tue", "wed", "thu", "fri", "sat"]
    assert captured[0]["working_hours"]["mon"] == ["10:00", "19:00"]
    # The `/calendar_service` alias may DM a one-time deprecation hint (13.03)
    # before the success DM, depending on whether the hint-sent table already
    # has an entry for this (project, operator). Assert against the LAST DM,
    # which is always the success message.
    assert "#5" in isolated_bot["dms"][-1][1]


def test_operator_service_add_api_error(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_upsert(**kwargs):
        raise httpx.RequestError("boom")

    monkeypatch.setattr(bot_main.api_client, "calendar_upsert_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_service add x 30 mon 09:00-18:00"),
    )
    assert response.json()["decision"] == "api_error"


def test_operator_service_remove(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_delete(*, project_id, rule_id, actor, actor_role, internal_token):
        captured.append({"rule_id": rule_id, "actor_role": actor_role})
        return {"deleted": True}

    monkeypatch.setattr(bot_main.api_client, "calendar_delete_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/calendar_service remove 5")
    )
    assert response.json()["decision"] == "removed"
    assert captured == [{"rule_id": 5, "actor_role": "operator"}]
    # /calendar_service alias may DM the 13.03 deprecation hint first; success is last.
    assert "#5" in isolated_bot["dms"][-1][1]


def test_operator_service_remove_api_error(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_delete(**kwargs):
        raise httpx.RequestError("boom")

    monkeypatch.setattr(bot_main.api_client, "calendar_delete_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/calendar_service remove 5")
    )
    assert response.json()["decision"] == "api_error"


def test_operator_service_usage_on_bad_input(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    client = TestClient(bot_app)
    # R1: name-only `/calendar_service add x` is now valid, so use an input
    # that still fails the relaxed parser — non-numeric duration token.
    response = client.post(
        "/telegram/webhook", json=_message(text="/calendar_service add x sixty")
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "usage"
    assert "Использование" in isolated_bot["dms"][0][1]


def test_operator_service_unknown_action_usage(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/calendar_service frobnicate")
    )
    assert response.json()["reason"] == "usage"


# --- parse_service_add unit branches ---------------------------------------


@pytest.mark.parametrize(
    "rest",
    [
        "remove x 60 mon 10:00-19:00",  # not 'add'
        "add x sixty mon 10:00-19:00",  # non-numeric duration
        "add x 0 mon 10:00-19:00",  # non-positive duration
        "add x 60 funday 10:00-19:00",  # bad day
        "add x 60 sat-mon 10:00-19:00",  # reversed day range
        "add x 60 xx-mon 10:00-19:00",  # bad start day in range
        "add x 60 mon noon",  # bad time
    ],
)
def test_parse_service_add_rejects(rest):
    assert parse_service_add(rest) is None


def test_parse_service_add_single_day():
    parsed = parse_service_add("add x 60 wed 10:00-19:00")
    assert parsed["service_days"] == ["wed"]


def test_parse_service_add_accepts_name_only():
    """R1 refinement: name-only is a catalog-only entry; trailing fields None."""
    parsed = parse_service_add("add маникюр")
    assert parsed == {
        "name": "маникюр",
        "duration_minutes": None,
        "service_days": None,
        "working_hours": None,
    }


def test_parse_service_add_accepts_name_and_duration():
    """R1 refinement: partial args (name + duration only) is valid."""
    parsed = parse_service_add("add маникюр 60")
    assert parsed == {
        "name": "маникюр",
        "duration_minutes": 60,
        "service_days": None,
        "working_hours": None,
    }


# --- admin /calendar_off @slug ---------------------------------------------
#
# There is no admin /calendar_on @slug command anymore — enablement happens
# in the operator's /connect_calendar OAuth callback. Disable + service config
# remain as admin paths.


def _stub_list_projects(monkeypatch, *, items):
    async def fake_list():
        return {"items": items}

    monkeypatch.setattr(bot_main.api_client, "list_projects", fake_list)


def test_admin_calendar_off_slug_disables(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=None)
    _stub_list_projects(
        monkeypatch, items=[{"id": _PROJECT_ID, "slug": "salon"}]
    )
    captured: list[dict] = []

    async def fake_disable(*, project_id, actor, actor_role, internal_token):
        captured.append({"actor_role": actor_role})
        return {"enabled": False}

    monkeypatch.setattr(bot_main.api_client, "calendar_disable", fake_disable)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_off salon", username="admin"),
    )
    body = response.json()
    assert body["route"] == "calendar_off"
    assert body["decision"] == "disabled"
    assert captured == [{"actor_role": "admin"}]
    assert "сохранён" in isolated_bot["dms"][0][1]


def test_admin_calendar_off_unknown_project(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=None)
    _stub_list_projects(monkeypatch, items=[])

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_off @ghost", username="admin"),
    )
    body = response.json()
    assert body["decision"] == "project_missing"
    assert "не найден" in isolated_bot["dms"][0][1]


def test_admin_calendar_off_api_error(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=None)
    _stub_list_projects(
        monkeypatch, items=[{"id": _PROJECT_ID, "slug": "salon"}]
    )

    async def fake_disable(*, project_id, actor, actor_role, internal_token):
        request = httpx.Request("POST", "http://api/x")
        response = httpx.Response(403, request=request)
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    monkeypatch.setattr(bot_main.api_client, "calendar_disable", fake_disable)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/calendar_off @salon", username="admin"),
    )
    body = response.json()
    assert body["status"] == "error"
    assert body["http_status"] == "403"
    assert "403" in isolated_bot["dms"][0][1]
