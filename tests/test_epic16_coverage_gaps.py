"""Targeted coverage for Epic 16 branches not hit by journey tests."""

from __future__ import annotations

import base64
import sqlite3
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

import services.api.app.main as api_main
import services.api.app.operator_registration as op_reg_module
from services.api.app.answerers import AnswerResult
from services.api.app.calendar.availability_answerer import RESPONSE_MODE_ESCALATION
from services.api.app.main import (
    InboundMessageRequest,
    hitl_ticket_repository,
    operator_registration_repository,
    operator_repository,
    project_repository,
    telegram_bot_sender,
    user_gateway_client,
)
from services.api.app.main import (
    app as api_app,
)
from services.api.app.operator_registration import OperatorRegistrationRepository
from services.api.app.operators import OperatorRepository, OperatorUsernameConflict
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.onboarding_callbacks import handle_onboarding_callback
from services.bot_gateway.app.operator_registration_callbacks import (
    handle_operator_registration_callback,
)
from services.bot_gateway.app.operator_telegram_link import (
    _send_qr_document,
    start_operator_telegram_link,
)
from services.bot_gateway.app.telegram_callback import NormalizedCallbackQuery
from services.bot_gateway.app.user_gateway_client import (
    UserGatewayClient,
    UserGatewayError,
    _extract_detail,
)


def _registration_wire(tmp_path) -> tuple[str, str]:
    internal = "internal-token"
    admin = "admin-internal-token"
    operator_db = str(tmp_path / "operators.sqlite3")
    projects_db = str(tmp_path / "projects.sqlite3")
    operator_repository.db_path = operator_db
    operator_repository.init_schema()
    operator_registration_repository.db_path = operator_db
    operator_registration_repository.init_schema()
    project_repository.db_path = projects_db
    project_repository.init_schema()
    project_repository.ensure_default_project()
    return internal, admin


@pytest.mark.asyncio
async def test_user_gateway_client_extract_detail_and_no_token_headers():
    bad_json = Mock()
    bad_json.json.side_effect = ValueError("not json")
    assert _extract_detail(bad_json) is None

    numeric_detail = Mock()
    numeric_detail.json.return_value = {"detail": 42}
    assert _extract_detail(numeric_detail) == "42"

    list_body = Mock()
    list_body.json.return_value = ["not", "a", "dict"]
    assert _extract_detail(list_body) is None

    client = UserGatewayClient(base_url="http://ug", internal_token="")
    assert client._headers() == {}


@pytest.mark.asyncio
async def test_user_gateway_client_extract_detail_non_dict_body():
    response = Mock()
    response.json.return_value = ["not-a-dict"]
    assert _extract_detail(response) is None


@pytest.mark.asyncio
async def test_user_gateway_client_verify_2fa_and_send_message(monkeypatch):
    response = Mock()
    response.raise_for_status = Mock()
    response.json.return_value = {"ok": True}
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.user_gateway_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    client = UserGatewayClient(base_url="http://ug", internal_token="tok")
    assert await client.verify_2fa(operator_id=1, password="pw") == {"ok": True}
    assert await client.send_message(operator_id=1, chat_id=2, text="hi") == {"ok": True}


@pytest.mark.asyncio
async def test_operator_telegram_link_branches(monkeypatch):
    user_gateway = AsyncMock()
    send_dm = AsyncMock()
    sender = AsyncMock()

    request = httpx.Request("POST", "http://ug/auth/qr_start")
    response = httpx.Response(409, request=request, json={"detail": "already_authenticated"})
    user_gateway.qr_start.side_effect = UserGatewayError(
        "err", request=request, response=response, detail="already_authenticated"
    )
    result = await start_operator_telegram_link(
        operator_id=1,
        operator_chat_id=10,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
    )
    assert result["decision"] == "already_authenticated"

    user_gateway.qr_start.side_effect = UserGatewayError(
        "err", request=request, response=response, detail="other"
    )
    result = await start_operator_telegram_link(
        operator_id=1,
        operator_chat_id=10,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
    )
    assert result["decision"] == "qr_start_failed"

    user_gateway.qr_start.side_effect = None
    user_gateway.qr_start.return_value = {}
    user_gateway.status.return_value = {"phase": "2fa_pending"}
    sleep_mock = AsyncMock()
    monkeypatch.setattr(
        "services.bot_gateway.app.operator_telegram_link.asyncio.sleep", sleep_mock
    )
    result = await start_operator_telegram_link(
        operator_id=1,
        operator_chat_id=10,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        max_polls=1,
        poll_interval_seconds=0.01,
    )
    assert result["decision"] == "awaiting_2fa"

    user_gateway.status.return_value = {"phase": "2fa_pending"}
    user_gateway.verify_2fa.side_effect = UserGatewayError(
        "err", request=request, response=response, detail="server_error"
    )

    async def _empty_password():
        return ""

    result = await start_operator_telegram_link(
        operator_id=1,
        operator_chat_id=10,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        max_polls=2,
        poll_interval_seconds=0.01,
        get_2fa_password=_empty_password,
    )
    assert result["decision"] == "timeout"

    user_gateway.verify_2fa.side_effect = UserGatewayError(
        "err", request=request, response=response, detail="server_error"
    )

    async def _password():
        return "pw"

    result = await start_operator_telegram_link(
        operator_id=1,
        operator_chat_id=10,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        max_polls=1,
        poll_interval_seconds=0.01,
        get_2fa_password=_password,
    )
    assert result["decision"] == "verify_2fa_failed"

    user_gateway.status.return_value = {"phase": "2fa_pending"}

    async def _empty_password():
        return ""

    result = await start_operator_telegram_link(
        operator_id=1,
        operator_chat_id=10,
        user_gateway_client=user_gateway,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        max_polls=2,
        poll_interval_seconds=0.01,
        get_2fa_password=_empty_password,
    )
    assert result["decision"] == "timeout"

    await _send_qr_document(
        qr_image_b64="not-valid-base64!!!",
        operator_chat_id=10,
        telegram_bot_sender=sender,
    )
    sender.send_document.assert_not_awaited()

    sender.send_document.side_effect = RuntimeError("send failed")
    await _send_qr_document(
        qr_image_b64=base64.b64encode(b"png").decode("ascii"),
        operator_chat_id=10,
        telegram_bot_sender=sender,
    )


def _op_reg_callback() -> NormalizedCallbackQuery:
    return NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq",
        chat_id=500,
        sender_username="@admin",
        sender_user_id=1,
        data="op_reg:approve:1",
        source_message_id=10,
    )


@pytest.mark.asyncio
async def test_op_reg_callback_network_errors_on_approve_and_reject():
    api = AsyncMock()
    api.approve_operator_register_request.side_effect = httpx.ConnectError("down")
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _op_reg_callback(),
        "approve",
        "1",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "api_error"

    api.reject_operator_register_request.side_effect = httpx.ReadError("down")
    result = await handle_operator_registration_callback(
        _op_reg_callback(),
        "reject",
        "1",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "api_error"


@pytest.mark.asyncio
async def test_op_reg_callback_reject_not_found_and_api_error():
    import services.bot_gateway.app.api_client as bg_api

    api = AsyncMock()
    request = httpx.Request("POST", "http://api")
    response = httpx.Response(404, request=request, json={"detail": "request_not_found"})
    api.reject_operator_register_request.side_effect = bg_api.ApiError(
        "err", request=request, response=response, detail="request_not_found"
    )
    send_dm = AsyncMock()
    sender = AsyncMock()
    result = await handle_operator_registration_callback(
        _op_reg_callback(),
        "reject",
        "1",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["reason"] == "request_not_found"

    response = httpx.Response(500, request=request, json={"detail": "boom"})
    api.reject_operator_register_request.side_effect = bg_api.ApiError(
        "err", request=request, response=response, detail="boom"
    )
    result = await handle_operator_registration_callback(
        _op_reg_callback(),
        "reject",
        "1",
        api_client=api,
        send_dm=send_dm,
        telegram_bot_sender=sender,
        admin_username="@admin",
    )
    assert result["decision"] == "api_error"


@pytest.mark.asyncio
async def test_onboarding_calendar_connect_failure_and_missing_url():
    api = AsyncMock()
    api.get_operator_by_id.return_value = {
        "id": 5,
        "username": "@op",
        "project_id": 77,
        "chat_id": 100,
    }
    api.initiate_calendar_connect.side_effect = httpx.ConnectError("down")
    send_dm = AsyncMock()
    result = await handle_onboarding_callback(
        NormalizedCallbackQuery(
            update_id=1,
            callback_query_id="cq",
            chat_id=100,
            sender_username="@op",
            sender_user_id=1,
            data="onboard:cal:5",
        ),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["decision"] == "calendar_connect_failed"

    api.initiate_calendar_connect.side_effect = None
    api.initiate_calendar_connect.return_value = {"consent_url": ""}
    result = await handle_onboarding_callback(
        NormalizedCallbackQuery(
            update_id=1,
            callback_query_id="cq",
            chat_id=100,
            sender_username="@op",
            sender_user_id=1,
            data="onboard:cal:5",
        ),
        "cal",
        "5",
        api_client=api,
        user_gateway_client=AsyncMock(),
        send_dm=send_dm,
        telegram_bot_sender=AsyncMock(),
        internal_token="tok",
    )
    assert result["decision"] == "calendar_missing_url"


def test_create_request_integrity_error_maps_to_pending_conflict(monkeypatch):
    registration_repo = OperatorRegistrationRepository(":memory:")
    calls: list[str] = []

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, parameters=()):
            calls.append(sql)
            if "INSERT INTO operator_registration_requests" in sql:
                raise sqlite3.IntegrityError("unique pending")
            if "SELECT status" in sql:
                return type("R", (), {"fetchone": lambda self: None})()
            return type("R", (), {"fetchone": lambda self: None})()

    monkeypatch.setattr(op_reg_module, "_connect", lambda _path: _Ctx())
    with pytest.raises(op_reg_module.RegistrationPendingConflict):
        registration_repo.create_request(username="@race", chat_id=1)


def test_approve_integrity_error_maps_to_username_conflict(tmp_path, monkeypatch):
    db_path = str(tmp_path / "operators.sqlite3")
    registration_repo = OperatorRegistrationRepository(db_path)
    operator_repo = OperatorRepository(db_path)
    request = registration_repo.create_request(username="@race2", chat_id=1)

    class _Ctx:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, parameters=()):
            if "INSERT INTO operators" in sql and "operator_registration" not in sql:
                raise sqlite3.IntegrityError("username taken")
            if "SELECT" in sql and "operator_registration_requests" in sql and "WHERE id" in sql:
                row = {
                    "status": "pending",
                    "username": "@race2",
                    "chat_id": 1,
                    "display_name": None,
                }
                return type("R", (), {"fetchone": lambda self: row})()
            if "SELECT 1 FROM operators" in sql:
                return type("R", (), {"fetchone": lambda self: None})()
            return type("R", (), {"fetchone": lambda self: None, "lastrowid": 9})()

    monkeypatch.setattr(op_reg_module, "_connect", lambda _path: _Ctx())
    with pytest.raises(OperatorUsernameConflict):
        registration_repo.approve(
            request_id=request.id,
            reviewed_by="@admin",
            project_id=1,
            operator_repository=operator_repo,
        )


def test_admin_registration_notify_chat_id_invalid_alert_falls_back_to_admin_operator(
    tmp_path, monkeypatch
):
    operator_repository.db_path = str(tmp_path / "operators.sqlite3")
    operator_repository.init_schema()
    project_repository.db_path = str(tmp_path / "projects.sqlite3")
    project_repository.init_schema()
    default = project_repository.ensure_default_project()
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", "bad")
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@admin_op")
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@other_admin")
    operator_repository.create(username="@admin_op", project_id=default.id, chat_id=888)
    assert api_main._admin_registration_notify_chat_id() == 888


def test_admin_registration_notify_chat_id_hitl_admin_fallback(tmp_path, monkeypatch):
    operator_repository.db_path = str(tmp_path / "operators.sqlite3")
    operator_repository.init_schema()
    project_repository.db_path = str(tmp_path / "projects.sqlite3")
    project_repository.init_schema()
    default = project_repository.ensure_default_project()
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@missing_admin")
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@hitl_admin")
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    operator_repository.create(
        username="@hitl_admin", project_id=default.id, chat_id=777
    )
    assert api_main._admin_registration_notify_chat_id() == 777


def test_admin_registration_notify_chat_id_invalid_runtime_chat(tmp_path, monkeypatch):
    operator_repository.db_path = str(tmp_path / "operators.sqlite3")
    operator_repository.init_schema()
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@missing_admin")
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@also_missing")
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: None)
    assert api_main._admin_registration_notify_chat_id() is None


def test_admin_registration_notify_chat_id_invalid_runtime_chat_non_int(tmp_path, monkeypatch):
    operator_repository.db_path = str(tmp_path / "operators.sqlite3")
    operator_repository.init_schema()
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@missing_admin")
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@also_missing")
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "not-int")
    assert api_main._admin_registration_notify_chat_id() is None


@pytest.mark.asyncio
async def test_send_reject_applicant_dm_when_request_missing(monkeypatch):
    monkeypatch.setattr(operator_registration_repository, "get", lambda _id: None)
    assert await api_main._send_reject_applicant_dm(request_id=1) is False


@pytest.mark.asyncio
async def test_send_reject_applicant_dm_skips_missing_chat_id(monkeypatch):
    monkeypatch.setattr(
        operator_registration_repository,
        "get",
        lambda _id: type("R", (), {"chat_id": None})(),
    )
    assert await api_main._send_reject_applicant_dm(request_id=1) is False


def test_reject_already_processed_via_api(tmp_path, monkeypatch):
    internal, admin = _registration_wire(tmp_path)
    monkeypatch.setattr(api_main.settings, "internal_service_token", internal)
    monkeypatch.setattr(api_main.settings, "admin_internal_token", admin)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)
    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal}"},
        json={"username": "@done", "chat_id": 1},
    )
    request_id = created.json()["request_id"]
    client.post(
        f"/operators/register-requests/{request_id}/reject",
        headers={"x-internal-token": admin},
    )
    twice = client.post(
        f"/operators/register-requests/{request_id}/reject",
        headers={"x-internal-token": admin},
    )
    assert twice.status_code == 409
    assert twice.json()["detail"] == "request_not_pending"


def test_reject_notify_not_found(tmp_path, monkeypatch):
    internal, _ = _registration_wire(tmp_path)
    monkeypatch.setattr(api_main.settings, "internal_service_token", internal)
    client = TestClient(api_app)
    response = client.post(
        "/operators/register-requests/999/reject-notify",
        headers={"Authorization": f"Bearer {internal}"},
    )
    assert response.status_code == 404


def test_approve_operator_username_conflict_via_api(tmp_path, monkeypatch):
    internal, admin = _registration_wire(tmp_path)
    monkeypatch.setattr(api_main.settings, "internal_service_token", internal)
    monkeypatch.setattr(api_main.settings, "admin_internal_token", admin)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)
    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal}"},
        json={"username": "@conflict", "chat_id": 2},
    )
    assert created.status_code == 200
    request_id = created.json()["request_id"]
    operator_repository.create(
        username="@conflict",
        project_id=project_repository.ensure_default_project().id,
        chat_id=1,
    )
    response = client.post(
        f"/operators/register-requests/{request_id}/approve",
        headers={"x-internal-token": admin},
        json={},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "operator_username_conflict"


def test_reject_not_found_and_get_operator_by_id_not_found(tmp_path, monkeypatch):
    internal, admin = _registration_wire(tmp_path)
    monkeypatch.setattr(api_main.settings, "internal_service_token", internal)
    monkeypatch.setattr(api_main.settings, "admin_internal_token", admin)
    client = TestClient(api_app)
    missing_reject = client.post(
        "/operators/register-requests/999/reject",
        headers={"x-internal-token": admin},
    )
    assert missing_reject.status_code == 404

    missing_op = client.get(
        "/operators/id/999",
        headers={"Authorization": f"Bearer {internal}"},
    )
    assert missing_op.status_code == 404


def test_onboarding_notify_operator_not_found(tmp_path, monkeypatch):
    internal, admin = _registration_wire(tmp_path)
    monkeypatch.setattr(api_main.settings, "internal_service_token", internal)
    monkeypatch.setattr(api_main.settings, "admin_internal_token", admin)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)
    created = client.post(
        "/operators/register-request",
        headers={"Authorization": f"Bearer {internal}"},
        json={"username": "@ghost", "chat_id": 1},
    )
    request_id = created.json()["request_id"]
    approved = client.post(
        f"/operators/register-requests/{request_id}/approve",
        headers={"x-internal-token": admin},
        json={},
    )
    assert approved.status_code == 200
    monkeypatch.setattr(operator_repository, "find_by_username", lambda username: None)
    response = client.post(
        f"/operators/register-requests/{request_id}/onboarding-notify",
        headers={"Authorization": f"Bearer {internal}"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "operator_not_found"


@pytest.mark.asyncio
async def test_approve_skips_onboarding_when_chat_id_missing(tmp_path, monkeypatch):
    from services.api.app.operator_registration_notify import (
        build_operator_registration_notifier,
    )

    sent: list[int] = []

    class _Sender:
        async def send_message(self, *, chat_id, text, reply_markup=None):
            sent.append(chat_id)
            return 1

    notify_admin, send_onboarding = build_operator_registration_notifier(
        telegram_sender=_Sender(),
        registration_repository=operator_registration_repository,
        admin_chat_id_getter=lambda: None,
    )
    from services.api.app.operators import Operator

    await send_onboarding(
        operator=Operator(
            id=1,
            username="@nochat",
            chat_id=None,
            project_id=1,
            display_name=None,
            is_active=True,
            created_at="t",
            updated_at="t",
        ),
        request_id=1,
    )
    assert sent == []
    await notify_admin(
        op_reg_module.RegistrationRequest(
            id=1,
            username="@x",
            chat_id=1,
            display_name=None,
            status="pending",
            project_id=None,
            created_at="t",
            reviewed_at=None,
            reviewed_by=None,
            rejection_cooldown_until=None,
        )
    )


def test_inbound_delivery_channel_validation(tmp_path, monkeypatch):
    _registration_wire(tmp_path)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    client = TestClient(api_app)
    empty = client.post("/conversations/inbound", json={"text": "   "})
    assert empty.status_code == 400

    bad_channel = client.post(
        "/conversations/inbound",
        json={"text": "hi", "delivery_channel": "sms"},
    )
    assert bad_channel.status_code == 422

    missing_op = client.post(
        "/conversations/inbound",
        json={"text": "hi", "delivery_channel": "operator_user"},
    )
    assert missing_op.status_code == 422


def test_resolve_project_for_inbound_operator_user_without_id(tmp_path):
    project_repository.db_path = str(tmp_path / "projects.sqlite3")
    project_repository.init_schema()
    project_repository.ensure_default_project()
    assert (
        api_main._resolve_project_for_inbound(
            chat_id=None,
            delivery_channel="operator_user",
            operator_id=None,
        )
        is not None
    )


@pytest.mark.asyncio
async def test_safe_send_message_operator_user_missing_id(tmp_path, monkeypatch):
    api_main.incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    result = await api_main._safe_send_message(
        chat_id=1,
        text="hi",
        failure_summary="x",
        failure_kind="x",
        delivery_channel="operator_user",
        operator_id=None,
    )
    assert result is False


@pytest.mark.asyncio
async def test_calendar_escalation_sends_ack_via_operator_user(tmp_path, monkeypatch):
    api_main.hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "inc.sqlite3")
    ug_send = AsyncMock()
    monkeypatch.setattr(api_main.user_gateway_client, "send_message", ug_send)
    monkeypatch.setattr(api_main, "_resolve_inbound_project_id", lambda chat_id: 1)
    monkeypatch.setattr(api_main, "_effective_hitl_operator_username", lambda: "@primary")
    notify = AsyncMock(return_value=True)
    monkeypatch.setattr(api_main, "_notify_hitl_operator_with_question", notify)

    request = InboundMessageRequest(
        text="свободно ли в субботу?",
        chat_id=5555,
        trace_id="t-op-user",
        delivery_channel="operator_user",
        operator_id=9,
    )
    result = await api_main._escalate_calendar_availability(
        request=request,
        trace_id="t-op-user",
        latency_ms=3,
        metadata={"calendar_operator": None, "reason": "calendar_not_connected"},
    )
    assert result["escalated"] is True
    ug_send.assert_awaited()


def test_conversations_inbound_calendar_escalation_path(tmp_path, monkeypatch):
    _registration_wire(tmp_path)
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))
    monkeypatch.setattr(
        api_main.answer_pipeline,
        "run",
        AsyncMock(
            return_value=AnswerResult(
                handled=True,
                response_mode=RESPONSE_MODE_ESCALATION,
                metadata={"calendar_operator": "@cal", "reason": "ambiguous"},
            )
        ),
    )
    escalated = AsyncMock(
        return_value={"escalated": True, "response_mode": "human_only", "trace_id": "t"}
    )
    monkeypatch.setattr(api_main, "_escalate_calendar_availability", escalated)
    client = TestClient(api_app)
    client.post(
        "/conversations/inbound",
        json={"text": "свободно?", "chat_id": 100},
    )
    escalated.assert_awaited_once()


def test_hitl_reply_operator_user_missing_operator_id(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    ticket = hitl_ticket_repository.create(
        conversation_ref="q",
        reason="awaiting_human_response",
        target_chat_id=100,
        delivery_channel="operator_user",
        operator_id=None,
    )
    hitl_ticket_repository.assign(ticket_id=ticket.id, operator_username="@op")
    monkeypatch.setattr(user_gateway_client, "send_message", AsyncMock())
    client = TestClient(api_app)
    response = client.post(
        f"/hitl/tickets/{ticket.id}/reply",
        json={"operator_username": "@op", "reply_text": "answer"},
    )
    assert response.status_code == 503


def test_bot_gateway_callback_rejected_payload(monkeypatch):
    from services.bot_gateway.app import main as bg_main

    bg_main.telegram_bot_sender.answer_callback_query = AsyncMock(return_value={"ok": True})
    client = TestClient(bot_app)
    bad = client.post(
        "/telegram/webhook",
        json={"update_id": 1, "callback_query": {"id": "", "from": {"id": 1}, "data": "x"}},
    )
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_bot_gateway_callback_handler_wrappers(monkeypatch):
    from services.bot_gateway.app import main as bg_main

    op_mock = AsyncMock(return_value={"route": "op_reg_callback"})
    on_mock = AsyncMock(return_value={"route": "onboard_callback"})
    monkeypatch.setattr(bg_main, "handle_operator_registration_callback", op_mock)
    monkeypatch.setattr(bg_main, "handle_onboarding_callback", on_mock)
    normalized = NormalizedCallbackQuery(
        update_id=1,
        callback_query_id="cq",
        chat_id=1,
        sender_username="@admin",
        sender_user_id=1,
        data="op_reg:approve:1",
    )
    assert await bg_main._handle_op_reg_callback(normalized, "approve", "1") == {
        "route": "op_reg_callback"
    }
    assert await bg_main._handle_onboard_callback(normalized, "cal", "1") == {
        "route": "onboard_callback"
    }


def test_bot_gateway_callback_handlers_and_duplicate_update(monkeypatch):
    from services.bot_gateway.app import main as bg_main

    bg_main.telegram_bot_sender.answer_callback_query = AsyncMock(return_value={"ok": True})
    op_reg = AsyncMock(return_value={"status": "accepted", "route": "op_reg_callback"})
    onboard = AsyncMock(return_value={"status": "accepted", "route": "onboard_callback"})
    monkeypatch.setitem(bg_main._CALLBACK_HANDLERS, "op_reg", op_reg)
    monkeypatch.setitem(bg_main._CALLBACK_HANDLERS, "onboard", onboard)

    client = TestClient(bot_app)
    duplicate = client.post(
        "/telegram/webhook",
        json={
            "update_id": 4242,
            "callback_query": {
                "id": "cq-dup",
                "from": {"id": 1, "username": "admin"},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "op_reg:approve:1",
            },
        },
    )
    assert duplicate.status_code == 200
    second = client.post(
        "/telegram/webhook",
        json={
            "update_id": 4242,
            "callback_query": {
                "id": "cq-dup",
                "from": {"id": 1, "username": "admin"},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "onboard:cal:1",
            },
        },
    )
    assert second.json()["reason"] == "duplicate_update"


def test_bot_gateway_callback_query_normalized_none(monkeypatch):
    from services.bot_gateway.app import main as bg_main

    bg_main.telegram_bot_sender.answer_callback_query = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(bg_main, "normalize_callback_query", lambda _payload: None)
    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json={
            "update_id": 5150,
            "callback_query": {
                "id": "cq-none",
                "from": {"id": 1, "username": "admin"},
                "message": {"message_id": 1, "chat": {"id": 1}},
                "data": "op_reg:approve:1",
            },
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


def test_bot_gateway_webhook_register_command_routes(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from platform_common.settings import get_settings
    from services.bot_gateway.app import main as bg_main

    db_path = tmp_path / "bot_gateway_register.sqlite3"
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(db_path))
    get_settings.cache_clear()

    monkeypatch.setattr(
        bg_main,
        "resolve_operator_for_sender",
        AsyncMock(return_value=None),
    )
    bg_main.api_client.create_operator_register_request = AsyncMock(
        return_value={"request_id": 12, "status": "pending"}
    )
    send_dm = AsyncMock()
    monkeypatch.setattr(bg_main, "_send_dm", send_dm)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json={
            "update_id": 4243,
            "message": {
                "message_id": 99,
                "from": {"id": 42, "username": "newbie", "first_name": "N"},
                "chat": {"id": 42, "type": "private"},
                "date": int(datetime.now(UTC).timestamp()),
                "text": "/register",
            },
        },
    )
    get_settings.cache_clear()
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "register_command"
    assert body["decision"] == "created"
    send_dm.assert_awaited_once()
