"""Unit tests for the new `/service` slash command (Epic 13, story 13.03).

Drives the bot_gateway webhook end-to-end with stubbed `ApiClient` methods and
a fake `_send_dm`. Covers:

- Start-of-message anchored regex (mid-message text does NOT trigger).
- key=value parser: standard tokens, quoted strings, Cyrillic dashes, 3-letter
  day codes, hours format.
- Operator gating: non-registered sender → `unauthorized_services` log, no DM,
  no `ApiClient` call.
- `/service add` happy path → POSTs to the canonical Epic-13 endpoint, DMs
  Russian confirmation.
- `/service add` 400 validation → DMs reason in Russian, no exception.
- `/service edit` (same handler as add) updates the row.
- `/service remove <name>` → resolves name to service_id via GET, then DELETE;
  admin → 403 → DMs operator-only message; missing → 404 → DMs not-found.
- `/service list` empty + populated.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.api_client import ApiError
from services.bot_gateway.app.calendar_commands import (
    ServiceKvError,
    _format_service_list_line,
    parse_service_kv,
)
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
    monkeypatch.setattr(
        bot_main.settings, "nl_ops_db_path", str(tmp_path / "nl_ops.db")
    )
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


# --- parse_service_kv tests ------------------------------------------------


def test_parse_service_kv_basic_keys():
    name, payload = parse_service_kv(
        "маникюр duration=60 days=mon-sat hours=10:00-19:00 "
        'price="от 2000" desc="классический и аппаратный" tags=classic,manicure'
    )
    assert name == "маникюр"
    assert payload["duration_minutes"] == 60
    assert payload["service_days"] == ["mon", "tue", "wed", "thu", "fri", "sat"]
    assert payload["working_hours"]["mon"] == ["10:00", "19:00"]
    assert payload["price_text"] == "от 2000"
    assert payload["description"] == "классический и аппаратный"
    assert payload["tags"] == ["classic", "manicure"]


def test_parse_service_kv_comma_days():
    name, payload = parse_service_kv("x days=mon,wed,fri")
    assert payload["service_days"] == ["mon", "wed", "fri"]


def test_parse_service_kv_multi_window_hours():
    _, payload = parse_service_kv("x hours=10:00-13:00,14:00-19:00 days=mon")
    assert payload["working_hours"]["mon"] == [
        ["10:00", "13:00"],
        ["14:00", "19:00"],
    ]


@pytest.mark.parametrize(
    "tokens",
    [
        "x days=пн–сб",  # en-dash
        "x days=пн-сб",  # ascii hyphen with Cyrillic codes
        "x days=пн—сб",  # em-dash
    ],
)
def test_parse_service_kv_cyrillic_dashes_normalize(tokens):
    _, payload = parse_service_kv(tokens)
    assert payload["service_days"] == ["mon", "tue", "wed", "thu", "fri", "sat"]


def test_parse_service_kv_cyrillic_days_comma():
    _, payload = parse_service_kv("x days=пн,ср,пт")
    assert payload["service_days"] == ["mon", "wed", "fri"]


def test_parse_service_kv_single_day_cyrillic():
    _, payload = parse_service_kv("x days=ср")
    assert payload["service_days"] == ["wed"]


def test_parse_service_kv_rejects_bad_duration():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x duration=полтора")
    assert info.value.reason == "invalid_duration"


def test_parse_service_kv_rejects_unknown_key():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x foo=bar")
    assert info.value.reason == "unknown_key"
    assert info.value.key == "foo"


def test_parse_service_kv_rejects_missing_name():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("duration=60")
    assert info.value.reason == "missing_name"


def test_parse_service_kv_rejects_bad_days_range():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x days=sat-mon")
    assert info.value.reason == "invalid_days"


def test_parse_service_kv_rejects_bad_days_comma():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x days=foo,bar")
    assert info.value.reason == "invalid_days"


def test_parse_service_kv_rejects_empty_days():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x days=,,,")
    assert info.value.reason == "invalid_days"


def test_parse_service_kv_rejects_bad_day_token():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x days=funday")
    assert info.value.reason == "invalid_days"


def test_parse_service_kv_rejects_bad_day_in_range():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x days=xx-mon")
    assert info.value.reason == "invalid_days"


def test_parse_service_kv_rejects_bad_hours():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x hours=noon")
    assert info.value.reason == "invalid_hours"


def test_parse_service_kv_rejects_empty_hours_value():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x hours=")
    assert info.value.reason == "invalid_hours"


def test_parse_service_kv_truncates_long_values():
    payload_str = "x desc=" + ("я" * 250)
    _, payload = parse_service_kv(payload_str)
    desc = payload["description"]
    assert desc.endswith("…")
    assert len(desc) == 201  # 200 chars + the truncation marker


def test_parse_service_kv_rejects_positional_after_kv():
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x duration=60 stray")
    assert info.value.reason == "unexpected_positional"


def test_parse_service_kv_unclosed_quote_still_parses():
    # Forgiving: an unterminated quoted value consumes the rest of the input.
    _, payload = parse_service_kv('x desc="forgot to close')
    assert payload["description"] == "forgot to close"


def test_parse_service_kv_blank_value_rejected_for_unknown_key():
    # ``=value`` with no key trips the unknown_key path.
    with pytest.raises(ServiceKvError) as info:
        parse_service_kv("x =bar")
    assert info.value.reason == "unknown_key"


def test_parse_service_kv_kv_only_no_name():
    # If the first token is a kv pair and no name follows, missing_name fires.
    with pytest.raises(ServiceKvError):
        parse_service_kv("duration=60 days=mon")


def test_parse_service_kv_hours_without_days_defaults_to_mon_fri():
    _, payload = parse_service_kv("x hours=10:00-19:00")
    assert set(payload["working_hours"].keys()) == {"mon", "tue", "wed", "thu", "fri"}


def test_parse_service_kv_empty_tags_after_strip():
    # All commas, no real tag content → empty list (whitespace-trimmed pieces
    # are dropped).
    _, payload = parse_service_kv('x tags=",,,"')
    assert payload["tags"] == []


# --- start-of-message anchoring -------------------------------------------


def test_service_command_mid_message_does_not_trigger(isolated_bot, monkeypatch):
    """Mid-message `/service add ...` MUST NOT trigger the command."""
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    upserts: list[dict] = []

    async def fake_upsert(**kwargs):
        upserts.append(kwargs)
        return {"id": 5, "name": "x"}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="что-то /service add маникюр duration=60"),
    )
    # Mid-message: NOT a slash command. The webhook falls through to operator
    # reply / forward, not the calendar handler.
    body = response.json()
    assert body.get("route") not in ("service",)
    assert upserts == []


def test_service_command_anchored_at_start(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_upsert(**kwargs):
        return {"id": 5, "name": "маникюр"}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add маникюр duration=60"),
    )
    assert response.json()["route"] == "service"


# --- operator gating ------------------------------------------------------


def test_service_non_registered_sender_ignored_no_dm(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=None)
    upserts: list[dict] = []

    async def fake_upsert(**kwargs):
        upserts.append(kwargs)
        return {"id": 0}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add x duration=60", username="rando"),
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "unauthorized_services"
    assert upserts == []
    assert isolated_bot["dms"] == []  # explicitly no DM


# --- /service add happy path ----------------------------------------------


def test_service_add_happy_path(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_upsert(**kwargs):
        captured.append(kwargs)
        # R1: include duration_minutes so this happy-path is "fully scheduled"
        # and the new catalog-only scheduling hint does NOT fire here.
        return {"id": 7, "name": "маникюр", "duration_minutes": 60}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(
            text=(
                "/service add маникюр duration=60 days=mon-sat "
                'hours=10:00-19:00 price="от 2000"'
            )
        ),
    )
    body = response.json()
    assert body["route"] == "service"
    assert body["decision"] == "added"
    assert captured[0]["project_id"] == _PROJECT_ID
    assert captured[0]["actor"] == _OPERATOR
    assert captured[0]["actor_role"] == "operator"
    assert captured[0]["payload"]["name"] == "маникюр"
    assert captured[0]["payload"]["duration_minutes"] == 60
    assert captured[0]["payload"]["price_text"] == "от 2000"
    # Russian DM confirmation; plain text (no Markdown control chars).
    assert "сохранена" in isolated_bot["dms"][0][1]
    assert "маникюр" in isolated_bot["dms"][0][1]
    assert "*" not in isolated_bot["dms"][0][1]
    assert "_" not in isolated_bot["dms"][0][1]


def test_service_slash_add_name_only(isolated_bot, monkeypatch):
    """R1: `/service add маникюр` (no other tokens) creates a catalog-only entry
    and the success DM appends the scheduling hint."""
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_upsert(**kwargs):
        captured.append(kwargs)
        return {"id": 7, "name": "маникюр", "duration_minutes": None}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add маникюр"),
    )
    body = response.json()
    assert body["route"] == "service"
    assert body["decision"] == "added"
    # Upsert payload contains ONLY name — no other keys.
    payload = captured[0]["payload"]
    assert payload == {"name": "маникюр"}
    dm = isolated_bot["dms"][-1][1]
    assert "сохранена" in dm
    assert "Чтобы сделать её бронируемой" in dm


def test_service_slash_add_full_omits_hint(isolated_bot, monkeypatch):
    """R1: When scheduling fields are present, the hint must be absent."""
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_upsert(**kwargs):
        return {"id": 8, "name": "маникюр", "duration_minutes": 60}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(
            text=(
                "/service add маникюр duration=60 days=mon-sat "
                "hours=10:00-19:00"
            )
        ),
    )
    assert response.json()["decision"] == "added"
    dm = isolated_bot["dms"][-1][1]
    assert "сохранена" in dm
    assert "Чтобы сделать её бронируемой" not in dm


def test_service_add_validation_400_dms_reason(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    request = httpx.Request("POST", "http://api/x")
    response_obj = httpx.Response(
        400, json={"detail": "invalid_duration"}, request=request
    )

    async def fake_upsert(**kwargs):
        raise ApiError(
            "validation",
            request=request,
            response=response_obj,
            detail="invalid_duration",
        )

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add маникюр duration=60"),
    )
    body = response.json()
    assert body["decision"] == "validation_failed"
    assert body["reason"] == "invalid_duration"
    dm = isolated_bot["dms"][0][1]
    assert "маникюр" in dm
    assert "invalid_duration" in dm
    assert "не сохранена" in dm


def test_service_add_kv_parse_error_dms(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    upserts: list[dict] = []

    async def fake_upsert(**kwargs):
        upserts.append(kwargs)
        return {"id": 0}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add маникюр duration=полтора"),
    )
    body = response.json()
    assert body["decision"] == "kv_parse_failed"
    assert body["reason"] == "invalid_duration"
    assert upserts == []
    assert "invalid_duration" in isolated_bot["dms"][0][1]


def test_service_add_api_error_5xx_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    request = httpx.Request("POST", "http://api/x")
    response_obj = httpx.Response(500, request=request)

    async def fake_upsert(**kwargs):
        raise ApiError(
            "boom",
            request=request,
            response=response_obj,
            detail=None,
        )

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add маникюр duration=60"),
    )
    body = response.json()
    assert body["decision"] == "api_error"
    assert "позже" in isolated_bot["dms"][0][1]


def test_service_add_network_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_upsert(**kwargs):
        raise httpx.RequestError("network down")

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add маникюр duration=60"),
    )
    assert response.json()["decision"] == "api_error"
    assert "позже" in isolated_bot["dms"][0][1]


def test_service_add_falls_back_to_kv_name_when_response_missing_name(
    isolated_bot, monkeypatch
):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_upsert(**kwargs):
        return {"id": 9}  # no name field

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text="/service add педикюр duration=60"),
    )
    assert response.json()["decision"] == "added"
    assert "педикюр" in isolated_bot["dms"][0][1]


# --- /service edit --------------------------------------------------------


def test_service_edit_uses_same_upsert(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())
    captured: list[dict] = []

    async def fake_upsert(**kwargs):
        captured.append(kwargs)
        return {"id": 7, "name": "маникюр"}

    monkeypatch.setattr(bot_main.api_client, "upsert_project_service", fake_upsert)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_message(text='/service edit маникюр price="от 2500"'),
    )
    body = response.json()
    assert body["decision"] == "edited"
    assert captured[0]["payload"]["price_text"] == "от 2500"


# --- /service remove ------------------------------------------------------


def test_service_remove_happy_path(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {
            "project_id": project_id,
            "services": [{"id": 12, "name": "маникюр"}],
        }

    delete_calls: list[dict] = []

    async def fake_delete(**kwargs):
        delete_calls.append(kwargs)
        return {"deleted": True}

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)
    monkeypatch.setattr(bot_main.api_client, "delete_project_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    body = response.json()
    assert body["decision"] == "removed"
    assert delete_calls[0]["service_id"] == 12
    assert delete_calls[0]["actor_role"] == "operator"
    assert "удалена" in isolated_bot["dms"][0][1]


def test_service_remove_admin_forbidden_403(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"services": [{"id": 12, "name": "маникюр"}]}

    request = httpx.Request("DELETE", "http://api/x")
    response_obj = httpx.Response(
        403, json={"detail": "admin_cannot_remove_service"}, request=request
    )

    async def fake_delete(**kwargs):
        raise ApiError(
            "forbidden",
            request=request,
            response=response_obj,
            detail="admin_cannot_remove_service",
        )

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)
    monkeypatch.setattr(bot_main.api_client, "delete_project_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    body = response.json()
    assert body["decision"] == "forbidden"
    assert "только оператору" in isolated_bot["dms"][0][1]


def test_service_remove_not_found_via_list_miss(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"services": []}

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    body = response.json()
    assert body["decision"] == "not_found"
    assert "не найдена" in isolated_bot["dms"][0][1]


def test_service_remove_skips_non_dict_entries_in_list(isolated_bot, monkeypatch):
    """Defensive: a malformed entry in the api response (non-dict) is skipped
    so the lookup falls through to the not-found branch instead of crashing."""
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"services": ["not a dict", None, 42]}

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    assert response.json()["decision"] == "not_found"


def test_service_remove_delete_404_after_resolve(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"services": [{"id": 12, "name": "маникюр"}]}

    request = httpx.Request("DELETE", "http://api/x")
    response_obj = httpx.Response(
        404, json={"detail": "project_service_not_found"}, request=request
    )

    async def fake_delete(**kwargs):
        raise ApiError(
            "missing",
            request=request,
            response=response_obj,
            detail="project_service_not_found",
        )

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)
    monkeypatch.setattr(bot_main.api_client, "delete_project_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    body = response.json()
    assert body["decision"] == "not_found"
    assert "не найдена" in isolated_bot["dms"][0][1]


def test_service_remove_other_api_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"services": [{"id": 12, "name": "маникюр"}]}

    request = httpx.Request("DELETE", "http://api/x")
    response_obj = httpx.Response(500, request=request)

    async def fake_delete(**kwargs):
        raise ApiError(
            "boom",
            request=request,
            response=response_obj,
            detail=None,
        )

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)
    monkeypatch.setattr(bot_main.api_client, "delete_project_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    assert response.json()["decision"] == "api_error"


def test_service_remove_list_network_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(**kwargs):
        raise httpx.RequestError("net")

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    assert response.json()["decision"] == "api_error"


def test_service_remove_delete_network_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"services": [{"id": 12, "name": "маникюр"}]}

    async def fake_delete(**kwargs):
        raise httpx.RequestError("network")

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)
    monkeypatch.setattr(bot_main.api_client, "delete_project_service", fake_delete)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service remove маникюр")
    )
    assert response.json()["decision"] == "api_error"


def test_service_remove_missing_name_usage(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/service remove"))
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "usage"
    assert "Использование" in isolated_bot["dms"][0][1]


# --- /service list --------------------------------------------------------


def test_service_list_empty(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {"project_id": project_id, "services": []}

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/service list"))
    body = response.json()
    assert body["decision"] == "list_empty"
    assert "не настроены" in isolated_bot["dms"][0][1]


def test_service_list_populated(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(*, project_id, internal_token):
        return {
            "services": [
                {
                    "id": 1,
                    "name": "маникюр",
                    "duration_minutes": 60,
                    "service_days": ["mon", "sat"],
                    "working_hours": {"mon": ["10:00", "19:00"]},
                    "price_text": "от 2000",
                },
                {"id": 2, "name": "педикюр"},
            ]
        }

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/service list"))
    body = response.json()
    assert body["decision"] == "listed"
    dm = isolated_bot["dms"][0][1]
    assert "Услуги проекта" in dm
    assert "маникюр" in dm
    assert "педикюр" in dm
    assert "60 мин" in dm


def test_service_list_api_error_dms_fallback(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    async def fake_list(**kwargs):
        raise httpx.RequestError("down")

    monkeypatch.setattr(bot_main.api_client, "list_project_services", fake_list)

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/service list"))
    assert response.json()["decision"] == "api_error"


def test_service_unknown_subcommand_dms_usage(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook", json=_message(text="/service frobnicate")
    )
    body = response.json()
    assert body["status"] == "ignored"
    assert body["reason"] == "usage"
    assert "Использование" in isolated_bot["dms"][0][1]


def test_service_bare_command_dms_usage(isolated_bot, monkeypatch):
    _stub_operator_lookup(monkeypatch, record=_registered_operator_record())

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=_message(text="/service"))
    body = response.json()
    assert body["status"] == "ignored"


# --- helper coverage ------------------------------------------------------


def test_format_service_list_line_handles_partial_fields():
    line = _format_service_list_line({"name": "минимум"})
    assert line == "• минимум"


def test_format_service_list_line_skips_bad_hours_shape():
    line = _format_service_list_line(
        {"name": "x", "working_hours": {"mon": "noon"}}
    )
    assert "noon" not in line  # not [start, end] pair => skipped


def test_format_service_list_line_missing_name_uses_placeholder():
    line = _format_service_list_line({"duration_minutes": 30})
    assert line.startswith("• ?")
