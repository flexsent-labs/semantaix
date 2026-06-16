import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from telethon.errors import PasswordHashInvalidError, SessionPasswordNeededError
from telethon.sessions import MemorySession

from platform_common.settings import AppSettings
from services.user_gateway.app.auth_session_repo import AuthSessionRepository
from services.user_gateway.app.auth_state import get_state, reset_all_states
from services.user_gateway.app.operator_auth_repo import OperatorTelegramAuthRepository
from services.user_gateway.app.routers import auth as auth_router
from services.user_gateway.app.telegram_auth import TelethonAuthService


class _ApiStub:
    async def get_operator(self, operator_id: int) -> dict | None:
        return {"id": operator_id}


class _FakeQrLogin:
    def __init__(self, *, wait_error: Exception | None = None) -> None:
        self.url = "tg://login?token=fake"
        self._wait_error = wait_error

    async def wait(self, timeout: int = 30) -> None:
        if self._wait_error is not None:
            raise self._wait_error

    async def recreate(self) -> None:
        return None


class _FakeClient:
    def __init__(self, *, qr_login: _FakeQrLogin) -> None:
        self.session = MemorySession()
        self.flood_sleep_threshold = 0
        self._qr_login = qr_login
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def qr_login(self) -> _FakeQrLogin:
        return self._qr_login

    async def sign_in(self, *, password: str) -> None:
        if password == "wrong":
            raise PasswordHashInvalidError(request=None)

    async def get_me(self):
        return SimpleNamespace(username="linked_operator")


@pytest.fixture(autouse=True)
def _reset_auth_state():
    reset_all_states()
    yield
    reset_all_states()


def _build_service(tmp_path, *, wait_error: Exception | None = None) -> TelethonAuthService:
    settings = AppSettings(
        telegram_api_id="1",
        telegram_api_hash="hash",
        user_gateway_db_path=str(tmp_path / "gateway.db"),
        tg_user_session_path=str(tmp_path / "legacy.session"),
        operator_sessions_dir=str(tmp_path / "sessions"),
        api_internal_base_url="http://api:8000",
    )
    auth_repo = AuthSessionRepository(settings.user_gateway_db_path)
    operator_repo = OperatorTelegramAuthRepository(settings.user_gateway_db_path)
    qr_login = _FakeQrLogin(wait_error=wait_error)
    client = _FakeClient(qr_login=qr_login)
    return TelethonAuthService(
        settings=settings,
        auth_session_repo=auth_repo,
        operator_auth_repo=operator_repo,
        api_client=_ApiStub(),
        client_factory=lambda _session_path: client,
    )


@pytest.mark.asyncio
async def test_qr_start_transitions_to_2fa_when_wait_requires_password(tmp_path):
    service = _build_service(
        tmp_path, wait_error=SessionPasswordNeededError(request=None)
    )

    payload = await service.qr_start(operator_id=11)
    await asyncio.sleep(0)

    state = get_state(11)
    assert "qr_image_b64" in payload
    assert payload["expires_in"] == 30
    assert state.phase == "2fa_pending"
    assert service.get_status(operator_id=11)["phase"] == "2fa_pending"


@pytest.mark.asyncio
async def test_verify_2fa_authenticates_after_pending_state(tmp_path):
    service = _build_service(tmp_path, wait_error=SessionPasswordNeededError(request=None))
    await service.qr_start(operator_id=22)
    await asyncio.sleep(0)

    result = await service.verify_2fa("correct", operator_id=22)

    assert result == {"status": "authenticated"}
    assert service.get_status(operator_id=22)["authenticated"] is True


@pytest.mark.asyncio
async def test_verify_2fa_rejects_invalid_password(tmp_path):
    service = _build_service(tmp_path, wait_error=SessionPasswordNeededError(request=None))
    await service.qr_start(operator_id=33)
    await asyncio.sleep(0)

    with pytest.raises(Exception) as exc:
        await service.verify_2fa("wrong", operator_id=33)
    assert getattr(exc.value, "status_code", None) == 401


def test_auth_router_qr_start_uses_dependency_override(tmp_path):
    class _RouterServiceStub:
        async def qr_start(self, operator_id=None):
            return {"operator_id": operator_id, "qr_image_b64": "abc", "expires_in": 30}

        def get_status(self, operator_id=None):
            return {"phase": "idle", "authenticated": False}

        async def verify_2fa(self, password: str, operator_id=None):
            return {"status": "ok", "password_len": str(len(password))}

    app = FastAPI()
    app.include_router(auth_router.router)
    app.dependency_overrides[auth_router.get_auth_service] = lambda: _RouterServiceStub()

    client = TestClient(app)
    start = client.post("/auth/qr_start?operator_id=5")
    status = client.get("/auth/status?operator_id=5")
    verify = client.post("/auth/verify_2fa?operator_id=5", json={"password": "secret"})

    assert start.status_code == 200
    assert start.json()["operator_id"] == 5
    assert status.status_code == 200
    assert verify.status_code == 200
    assert verify.json()["status"] == "ok"
