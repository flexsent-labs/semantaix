"""Epic 10 story 10.07: multi-operator resolution + sticky HITL routing."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiClient
from services.bot_gateway.app.operator_resolver import (
    resolve_operator_for_sender,
)

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10")]


@pytest.mark.story("10-07")
@pytest.mark.asyncio
async def test_bot_resolves_registered_operator_via_real_api(tmp_path, monkeypatch):
    from services.api.app import main as api_main
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    default = projects.ensure_default_project()
    operators.create(
        username="@op-b", project_id=default.id, chat_id=200
    )
    monkeypatch.setattr(api_main, "project_repository", projects)
    monkeypatch.setattr(api_main, "operator_repository", operators)

    transport = httpx.ASGITransport(app=api_main.app)

    class StubAsyncClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            kwargs.pop("timeout", None)
            super().__init__(transport=transport, base_url="http://api", timeout=5)

    monkeypatch.setattr(httpx, "AsyncClient", StubAsyncClient)

    client = ApiClient(base_url="http://api")
    resolved = await resolve_operator_for_sender(
        username="@op-b",
        api_client=client,
    )
    assert resolved is not None
    assert resolved.username == "@op-b"
    assert resolved.source == "registry"
    assert resolved.project_id == default.id

    # Unknown sender → None.
    none_result = await resolve_operator_for_sender(
        username="@stranger",
        api_client=client,
    )
    assert none_result is None


@pytest.mark.story("10-07")
@pytest.mark.asyncio
async def test_bot_is_fail_closed_when_api_is_down():
    """Fail-closed: api unreachable → resolver returns None regardless of username."""
    client = AsyncMock()
    client.find_operator_by_username.side_effect = httpx.ConnectError("down")
    resolved = await resolve_operator_for_sender(
        username="@any-operator",
        api_client=client,
    )
    assert resolved is None
