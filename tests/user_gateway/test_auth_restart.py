from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.user_gateway.app.auth_state import get_state, reset_all_states
from services.user_gateway.app.main import app, auth_service
from tests.user_gateway.conftest import FakeClient


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_all_states()


def test_verify_2fa_idle_returns_409() -> None:
    client = TestClient(app)
    response = client.post("/auth/verify_2fa", json={"password": "x"})
    assert response.status_code == 409


def test_verify_2fa_no_client_returns_409() -> None:
    state = get_state(None)
    state.phase = "2fa_pending"
    state.client = None
    client = TestClient(app)
    response = client.post("/auth/verify_2fa", json={"password": "x"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_verify_2fa_success() -> None:
    state = get_state(None)
    state.phase = "2fa_pending"
    state.client = FakeClient()
    result = await auth_service.verify_2fa("good", operator_id=None)
    assert result == {"status": "authenticated"}
