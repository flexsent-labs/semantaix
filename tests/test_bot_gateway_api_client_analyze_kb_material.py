"""``ApiClient.analyze_kb_material`` posts to the analyzer endpoint."""

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

    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


@pytest.mark.asyncio
async def test_analyze_kb_material_posts_to_correct_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = _http_mock(
        monkeypatch,
        response_json={"registered": True, "material_id": 9, "reason": "ok"},
    )
    client = ApiClient(base_url="http://api:8000")

    result = await client.analyze_kb_material(
        project_id=3,
        operator_file_short_id="ABCDEFGH",
        internal_token="bot-token-x",
    )

    assert result == {"registered": True, "material_id": 9, "reason": "ok"}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/sales/materials/analyze-kb-file"
    assert args.kwargs["json"] == {
        "project_id": 3,
        "operator_file_short_id": "ABCDEFGH",
    }
    assert args.kwargs["headers"] == {
        "Authorization": "Bearer bot-token-x"
    }


@pytest.mark.asyncio
async def test_analyze_kb_material_raises_api_error_on_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http_mock(monkeypatch, response_json={"detail": "x"}, status_code=502)
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError):
        await client.analyze_kb_material(
            project_id=1,
            operator_file_short_id="X",
            internal_token="t",
        )
