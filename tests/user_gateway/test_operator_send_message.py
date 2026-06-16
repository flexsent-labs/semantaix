from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.user_gateway.app.routers import messages


class _PoolStub:
    def __init__(self, *, connected: bool) -> None:
        self._connected = connected
        self.sent: list[tuple[int, int, str]] = []

    def is_connected(self, operator_id: int) -> bool:
        return self._connected and operator_id == 1

    async def send_message(self, operator_id: int, *, chat_id: int, text: str) -> None:
        self.sent.append((operator_id, chat_id, text))


def _build_client(pool: _PoolStub, monkeypatch) -> TestClient:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "secret")
    from platform_common.settings import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(messages.router)
    app.dependency_overrides[messages.get_client_pool] = lambda: pool
    return TestClient(app)


def test_send_message_rejects_without_internal_token(monkeypatch) -> None:
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    from platform_common.settings import get_settings

    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(messages.router)
    app.dependency_overrides[messages.get_client_pool] = lambda: _PoolStub(connected=True)
    client = TestClient(app)
    response = client.post(
        "/messages/send",
        json={"operator_id": 1, "chat_id": 123, "text": "hello"},
    )
    assert response.status_code == 401
    get_settings.cache_clear()


def test_send_message_returns_404_when_operator_not_connected(monkeypatch) -> None:
    client = _build_client(_PoolStub(connected=False), monkeypatch)
    response = client.post(
        "/messages/send",
        json={"operator_id": 1, "chat_id": 123, "text": "hello"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "operator_not_connected"


def test_send_message_success(monkeypatch) -> None:
    pool = _PoolStub(connected=True)
    client = _build_client(pool, monkeypatch)
    response = client.post(
        "/messages/send",
        json={"operator_id": 1, "chat_id": 123, "text": "hello"},
        headers={"Authorization": "Bearer secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "sent"}
    assert pool.sent == [(1, 123, "hello")]
