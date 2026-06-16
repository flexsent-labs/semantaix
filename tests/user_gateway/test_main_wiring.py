from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from platform_common.settings import AppSettings
from services.user_gateway.app.api_client import ApiClient
from services.user_gateway.app.auth_session_repo import AuthSessionRepository
from services.user_gateway.app.auth_state import get_state, reset_all_states
from services.user_gateway.app.main import (
    _default_client_factory,
    _handler_registrar,
    _on_authenticated,
    _router_factory,
    app,
    lifespan,
    operator_auth_repo,
    operator_client_pool,
)
from services.user_gateway.app.operator_auth_repo import (
    OperatorTelegramAuth,
    OperatorTelegramAuthRepository,
)
from services.user_gateway.app.routers.auth import get_auth_service
from services.user_gateway.app.routers.messages import get_client_pool, require_internal_token
from services.user_gateway.app.telegram_auth import TelethonAuthService
from tests.user_gateway.conftest import FakeClient, TimeoutQRLogin, TwoFaQRLogin


@pytest.mark.asyncio
async def test_api_client_paths() -> None:
    response_op = MagicMock()
    response_op.raise_for_status = MagicMock()
    response_op.json.return_value = {"id": 1}
    response_op.status_code = 200
    response_fwd = MagicMock()
    response_fwd.raise_for_status = MagicMock()
    response_fwd.json.return_value = {"ok": True}
    not_found = MagicMock()
    not_found.status_code = 404

    async def post(*_args, **_kwargs):
        return response_fwd

    async def get(url, **_kwargs):
        if "operators/9" in url:
            return not_found
        return response_op

    client = MagicMock()
    client.post = post
    client.get = get
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)

    with patch("services.user_gateway.app.api_client.httpx.AsyncClient", return_value=client):
        with_token = ApiClient(base_url="http://api", internal_token="tok")
        without_token = ApiClient(base_url="http://api", internal_token="")
        assert await with_token.get_operator(1) == {"id": 1}
        assert await without_token.get_operator(9) is None
        assert without_token._headers() == {}
        assert with_token._headers() == {"Authorization": "Bearer tok"}
        assert await with_token.forward_inbound(
            chat_id=1,
            text="hi",
            customer_username="c",
            trace_id="t",
            operator_id=2,
        ) == {"ok": True}


def test_main_wiring_helpers(tmp_path, monkeypatch) -> None:
    from services.user_gateway.app import main as gateway_main

    monkeypatch.setattr(gateway_main.settings, "telegram_api_id", "12345")
    monkeypatch.setattr(gateway_main.settings, "telegram_api_hash", "hash")
    client = _default_client_factory(str(tmp_path / "s.session"))
    assert client is not None
    assert _handler_registrar(client, lambda e: None) is not None
    assert _router_factory(1, "user")._operator_id == 1
    assert get_client_pool() is operator_client_pool


def test_main_handler_registrar_with_mock_client() -> None:
    class _Client:
        def on(self, _event):
            def decorator(handler):
                return handler

            return decorator

    assert _handler_registrar(_Client(), lambda _e: None) is not None


def test_require_internal_token_accepts_bearer(monkeypatch) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "secret")
    from platform_common.settings import get_settings

    get_settings.cache_clear()
    assert require_internal_token(authorization="Bearer secret") == "internal"
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_on_authenticated_paths() -> None:
    with patch.object(operator_client_pool, "start", new=AsyncMock()) as start:
        await _on_authenticated(None, "u")
        await _on_authenticated(99, "u")
        start.assert_not_awaited()
    record = OperatorTelegramAuth(
        operator_id=4,
        phase="authenticated",
        session_path="/data/4.session",
        linked_username="linked",
        customer_channel_active=False,
        started_at=1.0,
        updated_at=1.0,
    )
    with patch.object(operator_auth_repo, "get", return_value=record):
        with patch.object(operator_client_pool, "start", new=AsyncMock()) as start2:
            await _on_authenticated(4, "linked")
            start2.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_calls_stop_all() -> None:
    with patch.object(operator_client_pool, "stop_all", new=AsyncMock()) as stop_all:
        async with lifespan(app):
            pass
        stop_all.assert_awaited_once()


def test_get_auth_service_returns_singleton() -> None:
    assert get_auth_service() is get_auth_service()


@pytest.mark.asyncio
async def test_telethon_auth_remaining_branches(tmp_path) -> None:
    reset_all_states()
    settings = AppSettings(
        _env_file=None,
        user_gateway_db_path=str(tmp_path / "gateway.db"),
        tg_user_session_path=str(tmp_path / "legacy.session"),
        operator_sessions_dir=str(tmp_path / "sessions"),
        telegram_api_id="1",
        telegram_api_hash="hash",
        api_internal_base_url="http://api",
    )
    auth_repo = AuthSessionRepository(settings.user_gateway_db_path)
    operator_repo = OperatorTelegramAuthRepository(settings.user_gateway_db_path)
    service = TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
        api_client=AsyncMock(get_operator=AsyncMock(return_value={"id": 1})),
        client_factory=lambda _p: FakeClient(),
        on_authenticated=AsyncMock(),
    )
    await service.qr_start(operator_id=None)
    await service._await_qr_scan(None)
    assert get_state(None).phase == "authenticated"

    reset_all_states()
    service4 = TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
        api_client=AsyncMock(),
        client_factory=lambda _p: FakeClient(qr_login=TimeoutQRLogin()),
    )
    await service4.qr_start(operator_id=None)
    await service4._await_qr_scan(None)

    reset_all_states()
    service5 = TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
        api_client=AsyncMock(get_operator=AsyncMock(return_value={"id": 3})),
        client_factory=lambda _p: FakeClient(qr_login=TwoFaQRLogin()),
    )
    await service5.qr_start(operator_id=3)
    await service5._await_qr_scan(3)
    assert operator_repo.get_phase(3) == "2fa_pending"

    reset_all_states()
    service5b = TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
        api_client=AsyncMock(),
        client_factory=lambda _p: FakeClient(qr_login=TwoFaQRLogin()),
    )
    await service5b.qr_start(operator_id=None)
    await service5b._await_qr_scan(None)
    assert auth_repo.get_phase() == "2fa_pending"

    state_early = get_state(None)
    state_early.qr_login = None
    await service._await_qr_scan(None)

    with pytest.raises(Exception) as exc:
        await TelethonAuthService(
            settings=settings,
            api_client=AsyncMock(get_operator=AsyncMock(return_value=None)),
        ).qr_start(operator_id=5)
    assert exc.value.status_code == 404

    reset_all_states()
    get_state(None).phase = "authenticated"
    with pytest.raises(Exception) as exc2:
        await service.qr_start(operator_id=None)
    assert exc2.value.status_code == 409

    reset_all_states()
    class ErrorQR(TwoFaQRLogin):
        async def wait(self, timeout: float = 30) -> None:
            raise RuntimeError("down")

    reset_all_states()
    service7 = TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
        api_client=AsyncMock(),
        client_factory=lambda _p: FakeClient(qr_login=ErrorQR()),
    )
    await service7.qr_start(operator_id=None)
    await service7._await_qr_scan(None)
    assert get_state(None).phase == "idle"

    reset_all_states()
    auth_repo.set_phase("qr_pending")
    operator_repo.upsert(operator_id=50, phase="2fa_pending", session_path="/50")
    TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
    ).clear_stale_on_startup()

    state = get_state(88)
    state.client = FakeClient()
    await service._mark_authenticated(88, state)

    bare = TelethonAuthService(settings=settings)
    client = bare._default_client_factory(str(tmp_path / "bare.session"))
    assert client is not None
