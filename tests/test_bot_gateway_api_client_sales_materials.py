"""``ApiClient`` methods for the Story 12.05 client-materials endpoints.

Direct unit tests for the wire shape of ``add_sales_material`` /
``list_sales_materials`` / ``delete_sales_material`` /
``dispatch_sales_material``. The dispatcher tests in
``tests/test_bot_gateway_material_command.py`` cover behaviour through a
fake api; these tests pin the HTTP path + headers + payload so a
refactor cannot silently flip the contract the api endpoints expect.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiClient, ApiError


def _http_mock(monkeypatch, *, response_json: dict, status_code: int = 200):
    response = Mock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = response_json
    if status_code >= 400:
        request = httpx.Request("POST", "http://api/x")
        response.raise_for_status = Mock(
            side_effect=httpx.HTTPStatusError(
                "boom",
                request=request,
                response=httpx.Response(status_code, request=request),
            )
        )
    else:
        response.raise_for_status = Mock()

    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    http_client.get = AsyncMock(return_value=response)
    http_client.delete = AsyncMock(return_value=response)

    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


@pytest.mark.asyncio
async def test_add_sales_material_posts_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"id": 17})
    client = ApiClient(base_url="http://api:8000")
    result = await client.add_sales_material(
        project_id=7,
        kind="video",
        local_path="/var/sm/vid.mp4",
        byte_size=2048,
        internal_token="bot-tok",
        duration_seconds=10,
        caption="Тур-превью",
        tags=["tour_preview"],
        telegram_file_id="TG-VID-1",
        source_operator_file_id="op-file-1",
    )
    assert result == {"id": 17}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/sales/materials"
    assert args.kwargs["json"] == {
        "project_id": 7,
        "kind": "video",
        "local_path": "/var/sm/vid.mp4",
        "byte_size": 2048,
        "duration_seconds": 10,
        "caption": "Тур-превью",
        "tags": ["tour_preview"],
        "telegram_file_id": "TG-VID-1",
        "source_operator_file_id": "op-file-1",
    }
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_add_sales_material_raises_api_error_with_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = Mock(spec=httpx.Response)
    response.status_code = 400
    response.json.return_value = {"detail": "caption_too_long"}
    request = httpx.Request("POST", "http://api/x")
    response.raise_for_status = Mock(
        side_effect=httpx.HTTPStatusError(
            "boom",
            request=request,
            response=httpx.Response(
                400,
                request=request,
                content=b'{"detail": "caption_too_long"}',
            ),
        )
    )
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as exc:
        await client.add_sales_material(
            project_id=7,
            kind="video",
            local_path="/x",
            byte_size=1,
            internal_token="t",
        )
    assert exc.value.detail == "caption_too_long"


@pytest.mark.asyncio
async def test_list_sales_materials_uses_get_with_project_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"materials": []})
    client = ApiClient(base_url="http://api:8000")
    result = await client.list_sales_materials(
        project_id=7, internal_token="bot-tok"
    )
    assert result == {"materials": []}
    args = http.get.await_args
    assert args.args[0] == "http://api:8000/sales/materials"
    assert args.kwargs["params"] == {"project_id": 7}
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_delete_sales_material_uses_delete_verb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(monkeypatch, response_json={"ok": True})
    client = ApiClient(base_url="http://api:8000")
    result = await client.delete_sales_material(
        material_id=17, internal_token="bot-tok"
    )
    assert result == {"ok": True}
    args = http.delete.await_args
    assert args.args[0] == "http://api:8000/sales/materials/17"
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_dispatch_sales_material_posts_chat_and_material_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(
        monkeypatch,
        response_json={"ok": True, "telegram_file_id_cached": False},
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.dispatch_sales_material(
        chat_id=42,
        material_id=17,
        internal_token="bot-tok",
        caption_override="custom caption",
        trace_id="trc-xyz",
    )
    assert result == {"ok": True, "telegram_file_id_cached": False}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/sales/dispatch/material"
    assert args.kwargs["json"] == {
        "chat_id": 42,
        "material_id": 17,
        "caption_override": "custom caption",
        "trace_id": "trc-xyz",
    }
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-tok"}


@pytest.mark.asyncio
async def test_dispatch_sales_material_defaults_omit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(
        monkeypatch,
        response_json={"ok": True, "telegram_file_id_cached": True},
    )
    client = ApiClient(base_url="http://api:8000")
    await client.dispatch_sales_material(
        chat_id=1,
        material_id=2,
        internal_token="t",
    )
    args = http.post.await_args
    assert args.kwargs["json"] == {
        "chat_id": 1,
        "material_id": 2,
        "caption_override": None,
        "trace_id": None,
    }
