import asyncio

import pytest

from services.user_gateway.app.operator_auth_repo import OperatorTelegramAuthRepository
from services.user_gateway.app.operator_client_pool import OperatorClientPool


class _FakeClient:
    def __init__(self) -> None:
        self.connected = False
        self.sent: list[tuple[int, str]] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


class _FakeRouter:
    def __init__(self) -> None:
        self.handled: list[object] = []
        self._waiter = asyncio.Event()

    async def handle_new_message(self, event: object) -> None:
        self.handled.append(event)

    async def drain_queue(self) -> None:
        while True:
            await self._waiter.wait()


@pytest.mark.asyncio
async def test_operator_client_pool_start_send_stop(tmp_path):
    repo = OperatorTelegramAuthRepository(str(tmp_path / "gateway.db"))
    repo.upsert(
        operator_id=70,
        phase="authenticated",
        session_path=str(tmp_path / "sessions" / "70.session"),
    )
    client = _FakeClient()
    router = _FakeRouter()
    handlers: list[object] = []

    pool = OperatorClientPool(
        operator_auth_repo=repo,
        client_factory=lambda _path: client,
        handler_registrar=lambda _client, handler: handlers.append(handler),
        router_factory=lambda _id, _name: router,
    )

    await pool.start(70, session_path=str(tmp_path / "sessions" / "70.session"))
    await pool.send_message(70, chat_id=777, text="hello")
    await pool.stop(70)

    assert pool.get_client(70) is None
    assert handlers
    assert client.sent == [(777, "hello")]
    assert repo.get(70) is not None
    assert repo.get(70).customer_channel_active is False


@pytest.mark.asyncio
async def test_operator_client_pool_send_missing_operator_raises(tmp_path):
    repo = OperatorTelegramAuthRepository(str(tmp_path / "gateway.db"))
    pool = OperatorClientPool(
        operator_auth_repo=repo,
        client_factory=lambda _path: _FakeClient(),
        handler_registrar=lambda _client, _handler: None,
        router_factory=lambda _id, _name: _FakeRouter(),
    )

    with pytest.raises(KeyError):
        await pool.send_message(500, chat_id=1, text="x")


@pytest.mark.asyncio
async def test_operator_client_pool_idempotent_start(tmp_path):
    repo = OperatorTelegramAuthRepository(str(tmp_path / "gateway.db"))
    repo.upsert(operator_id=71, phase="authenticated", session_path="/s/71.session")
    client = _FakeClient()
    pool = OperatorClientPool(
        operator_auth_repo=repo,
        client_factory=lambda _path: client,
        handler_registrar=lambda _client, _handler: None,
        router_factory=lambda _id, _name: _FakeRouter(),
    )
    await pool.start(71, session_path=str(tmp_path / "71.session"))
    await pool.start(71, session_path=str(tmp_path / "71.session"))
    assert pool.is_connected(71)
    assert not pool.is_connected(999)
    await pool.stop_all()
    assert pool.get_client(71) is None
