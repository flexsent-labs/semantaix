from __future__ import annotations

from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from services.bot_gateway.app.api_client import ApiClient, ApiError


def _http_mock(monkeypatch, *, response_json: dict):
    response = Mock()
    response.json.return_value = response_json
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
async def test_forward_inbound_posts_to_correct_endpoint(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"escalated": True})
    client = ApiClient(base_url="http://api:8000")
    result = await client.forward_inbound(
        text="hi",
        chat_id=1,
        customer_username="@c",
        trace_id="t-1",
    )
    assert result == {"escalated": True}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/conversations/inbound"
    assert args.kwargs["json"] == {
        "text": "hi",
        "chat_id": 1,
        "customer_username": "@c",
        "trace_id": "t-1",
    }


@pytest.mark.asyncio
async def test_forward_inbound_passes_timeout_override(monkeypatch):
    # Story 12.24 — a long per-call timeout so a slow sales-escalation turn
    # isn't mistaken for a failure and retried mid-send.
    captured: dict = {}
    response = Mock()
    response.json.return_value = {"ok": True}
    response.raise_for_status = Mock()
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None

    def _factory(timeout):
        captured["timeout"] = timeout
        return cm

    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient", _factory
    )
    client = ApiClient(base_url="http://api:8000", timeout_seconds=10)
    await client.forward_inbound(
        text="hi", chat_id=1, customer_username="@c", trace_id="t", timeout_seconds=45
    )
    assert captured["timeout"] == 45


@pytest.mark.asyncio
async def test_forward_inbound_defaults_to_client_timeout(monkeypatch):
    captured: dict = {}
    response = Mock()
    response.json.return_value = {"ok": True}
    response.raise_for_status = Mock()
    http_client = AsyncMock()
    http_client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None

    def _factory(timeout):
        captured["timeout"] = timeout
        return cm

    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient", _factory
    )
    client = ApiClient(base_url="http://api:8000", timeout_seconds=10)
    await client.forward_inbound(
        text="hi", chat_id=1, customer_username="@c", trace_id="t"
    )
    assert captured["timeout"] == 10


@pytest.mark.asyncio
async def test_deliver_operator_reply_posts_to_correct_endpoint(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"delivered": True})
    client = ApiClient(base_url="http://api:8000/")
    result = await client.deliver_operator_reply(
        ticket_id=42, operator_username="@op", reply_text="hello"
    )
    assert result == {"delivered": True}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/hitl/tickets/42/reply"
    assert args.kwargs["json"] == {
        "operator_username": "@op",
        "reply_text": "hello",
    }


@pytest.mark.asyncio
async def test_submit_operator_upload_posts_to_correct_endpoint(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"candidate_id": 7})
    client = ApiClient(base_url="http://api:8000")
    result = await client.submit_operator_upload(
        operator_username="@op",
        source_file_type="pdf",
        source_file_name="schedule.pdf",
        stored_binary_path="/data/x.pdf",
        is_confidential=True,
        inline_text=None,
        timeout_seconds=42,
    )
    assert result == {"candidate_id": 7}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/knowledge/operator_upload"
    assert args.kwargs["json"] == {
        "operator_username": "@op",
        "source_file_type": "pdf",
        "source_file_name": "schedule.pdf",
        "stored_binary_path": "/data/x.pdf",
        "is_confidential": True,
        "inline_text": None,
        "operator_short_id": None,
    }


@pytest.mark.asyncio
async def test_submit_operator_upload_uses_default_timeout(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"candidate_id": 1})
    client = ApiClient(base_url="http://api:8000", timeout_seconds=5)
    await client.submit_operator_upload(
        operator_username="@op",
        source_file_type="inline_text",
        source_file_name=None,
        stored_binary_path=None,
        is_confidential=False,
        inline_text="hello",
    )
    assert http.post.await_count == 1


@pytest.mark.asyncio
async def test_set_persona_posts_minimal_payload(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"first_name": "Анна", "last_name": "Иванова"})
    client = ApiClient(base_url="http://api:8000")
    result = await client.set_persona(
        first_name="Анна", last_name="Иванова", updated_by="@ajdevy"
    )
    assert result == {"first_name": "Анна", "last_name": "Иванова"}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/hitl/runtime-config/persona"
    assert args.kwargs["json"] == {
        "first_name": "Анна",
        "last_name": "Иванова",
        "updated_by": "@ajdevy",
    }


def _http_get_mock(monkeypatch, *, status_code: int, response_json: dict | None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = response_json
    response.raise_for_status = Mock()

    http_client = AsyncMock()
    http_client.get = AsyncMock(return_value=response)

    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


@pytest.mark.asyncio
async def test_fetch_file_inspect_passes_bearer_and_as_user(monkeypatch):
    http = _http_get_mock(
        monkeypatch,
        status_code=200,
        response_json={"short_id": "X", "candidate_text": "t"},
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.fetch_file_inspect(
        short_id="X", requester_username="@alice", internal_token="bot-token"
    )
    assert result == {"short_id": "X", "candidate_text": "t"}
    args = http.get.await_args
    assert args.args[0] == "http://api:8000/admin/files/X"
    assert args.kwargs["params"] == {"as_user": "@alice"}
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-token"}


@pytest.mark.asyncio
async def test_fetch_file_inspect_returns_none_on_404(monkeypatch):
    _http_get_mock(monkeypatch, status_code=404, response_json=None)
    client = ApiClient(base_url="http://api:8000")
    result = await client.fetch_file_inspect(
        short_id="MISSING", requester_username="@alice", internal_token="t"
    )
    assert result is None


@pytest.mark.asyncio
async def test_search_files_passes_query_and_limit(monkeypatch):
    http = _http_get_mock(
        monkeypatch, status_code=200, response_json={"total": 0, "items": []}
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.search_files(
        query="договор",
        requester_username="@alice",
        internal_token="bot-token",
        limit=5,
    )
    assert result == {"total": 0, "items": []}
    args = http.get.await_args
    assert args.args[0] == "http://api:8000/admin/files/search"
    assert args.kwargs["params"] == {
        "q": "договор",
        "as_user": "@alice",
        "limit": 5,
    }
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-token"}


def _error_response(
    *,
    status_code: int,
    body: object,
    content_type: str = "application/json",
) -> httpx.Response:
    if isinstance(body, (dict, list)):
        import json as _json

        content = _json.dumps(body).encode("utf-8")
    elif isinstance(body, bytes):
        content = body
    else:
        content = str(body).encode("utf-8")
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": content_type},
        content=content,
        request=httpx.Request("POST", "http://api:8000/some/path"),
    )


def _http_error_mock(monkeypatch, *, response: httpx.Response, method: str = "post"):
    http_client = AsyncMock()
    setattr(http_client, method, AsyncMock(return_value=response))

    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return http_client


@pytest.mark.asyncio
async def test_post_raises_api_error_with_detail_when_json_body(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=422, body={"detail": "empty_text"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.submit_operator_upload(
            operator_username="@op",
            source_file_type="pdf",
            source_file_name="x.pdf",
            stored_binary_path="/data/x.pdf",
            is_confidential=False,
        )
    assert info.value.detail == "empty_text"
    assert info.value.response.status_code == 422
    assert isinstance(info.value, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_post_api_error_detail_is_none_when_body_not_json(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(
            status_code=500,
            body=b"<html>boom</html>",
            content_type="text/html",
        ),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.deliver_operator_reply(
            ticket_id=1, operator_username="@op", reply_text="hi"
        )
    assert info.value.detail is None
    assert info.value.response.status_code == 500


@pytest.mark.asyncio
async def test_post_api_error_detail_is_none_when_detail_field_missing(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=400, body={"error": "nope"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.forward_inbound(
            text="x", chat_id=1, customer_username=None, trace_id="t"
        )
    assert info.value.detail is None


@pytest.mark.asyncio
async def test_post_api_error_stringifies_non_string_detail(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(
            status_code=422,
            body={"detail": {"loc": ["body"], "msg": "field required"}},
        ),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.forward_inbound(
            text="x", chat_id=1, customer_username=None, trace_id="t"
        )
    assert info.value.detail is not None
    assert "field required" in info.value.detail


@pytest.mark.asyncio
async def test_get_raises_api_error_with_detail(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=404, body={"detail": "not_found"}),
        method="get",
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.list_projects()
    assert info.value.detail == "not_found"


@pytest.mark.asyncio
async def test_patch_raises_api_error_with_detail(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=409, body={"detail": "conflict"}),
        method="patch",
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.detach_operator(username="@op")
    assert info.value.detail == "conflict"


@pytest.mark.asyncio
async def test_fetch_file_inspect_raises_api_error_on_non_404(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=500, body={"detail": "boom"}),
        method="get",
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.fetch_file_inspect(
            short_id="X", requester_username="@u", internal_token="t"
        )
    assert info.value.detail == "boom"


@pytest.mark.asyncio
async def test_search_files_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=400, body={"detail": "bad_query"}),
        method="get",
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.search_files(
            query="x", requester_username="@u", internal_token="t"
        )
    assert info.value.detail == "bad_query"


@pytest.mark.asyncio
async def test_find_operator_by_username_returns_none_on_404(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=404, body={"detail": "missing"}),
        method="get",
    )
    client = ApiClient(base_url="http://api:8000")
    assert await client.find_operator_by_username(username="@nope") is None


@pytest.mark.asyncio
async def test_find_operator_by_username_reraises_non_404(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=500, body={"detail": "oops"}),
        method="get",
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.find_operator_by_username(username="@op")
    assert info.value.response.status_code == 500
    assert info.value.detail == "oops"


@pytest.mark.asyncio
async def test_initiate_calendar_connect_posts_with_bearer(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"consent_url": "https://c"})
    client = ApiClient(base_url="http://api:8000")
    result = await client.initiate_calendar_connect(
        project_id=11, operator="@op", internal_token="svc-token"
    )
    assert result == {"consent_url": "https://c"}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/calendar/connect/initiate"
    assert args.kwargs["json"] == {"project_id": 11, "operator": "@op"}
    assert args.kwargs["headers"] == {"Authorization": "Bearer svc-token"}


@pytest.mark.asyncio
async def test_initiate_calendar_connect_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(
            status_code=400, body={"detail": "not_calendar_operator"}
        ),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.initiate_calendar_connect(
            project_id=11, operator="@op", internal_token="t"
        )
    assert info.value.detail == "not_calendar_operator"


@pytest.mark.asyncio
async def test_disconnect_calendar_posts_with_bearer(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"disconnected": True})
    client = ApiClient(base_url="http://api:8000")
    result = await client.disconnect_calendar(
        project_id=11, operator="@op", internal_token="svc-token"
    )
    assert result == {"disconnected": True}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/calendar/disconnect"
    assert args.kwargs["json"] == {"project_id": 11, "operator": "@op"}
    assert args.kwargs["headers"] == {"Authorization": "Bearer svc-token"}


@pytest.mark.asyncio
async def test_disconnect_calendar_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(
            status_code=503, body={"detail": "calendar_oauth_not_configured"}
        ),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.disconnect_calendar(
            project_id=11, operator="@op", internal_token="t"
        )
    assert info.value.detail == "calendar_oauth_not_configured"


def _http_method_mock(monkeypatch, *, method: str, response_json: dict):
    response = Mock()
    response.json.return_value = response_json
    response.raise_for_status = Mock()

    http_client = AsyncMock()
    setattr(http_client, method, AsyncMock(return_value=response))

    cm = AsyncMock()
    cm.__aenter__.return_value = http_client
    cm.__aexit__.return_value = None
    monkeypatch.setattr(
        "services.bot_gateway.app.api_client.httpx.AsyncClient",
        lambda timeout: cm,
    )
    return getattr(http_client, method)


# --- story 11.08 calendar config methods ----------------------------------
#
# No `calendar_enable` ApiClient method anymore — enablement is implicit in
# the operator's /connect_calendar OAuth callback (PR #75 follow-up).


@pytest.mark.asyncio
async def test_calendar_disable_posts(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"enabled": False})
    client = ApiClient(base_url="http://api:8000")
    result = await client.calendar_disable(
        project_id=11, actor="@op", actor_role="operator", internal_token="svc"
    )
    assert result == {"enabled": False}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/calendar/projects/11/disable"
    assert args.kwargs["json"] == {"actor": "@op", "actor_role": "operator"}


@pytest.mark.asyncio
async def test_calendar_disable_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=403, body={"detail": "x"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError):
        await client.calendar_disable(
            project_id=11, actor="@op", actor_role="admin", internal_token="t"
        )


@pytest.mark.asyncio
async def test_calendar_get_settings_uses_get(monkeypatch):
    get = _http_method_mock(
        monkeypatch, method="get", response_json={"enabled": True}
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.calendar_get_settings(project_id=11, internal_token="svc")
    assert result == {"enabled": True}
    args = get.await_args
    assert args.args[0] == "http://api:8000/calendar/projects/11/settings"
    assert args.kwargs["headers"] == {"Authorization": "Bearer svc"}


@pytest.mark.asyncio
async def test_calendar_get_settings_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        method="get",
        response=_error_response(status_code=401, body={"detail": "x"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError):
        await client.calendar_get_settings(project_id=11, internal_token="t")


@pytest.mark.asyncio
async def test_calendar_upsert_service_posts_full_payload(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"id": 7})
    client = ApiClient(base_url="http://api:8000")
    result = await client.calendar_upsert_service(
        project_id=11,
        actor="@op",
        actor_role="operator",
        internal_token="svc",
        rule_id=3,
        name="x",
        duration_minutes=60,
        working_hours={"mon": ["09:00", "18:00"]},
        service_days=["mon"],
        date_exceptions=["2026-01-01"],
    )
    assert result == {"id": 7}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/calendar/projects/11/services"
    assert args.kwargs["json"]["rule_id"] == 3
    assert args.kwargs["json"]["working_hours"] == {"mon": ["09:00", "18:00"]}


@pytest.mark.asyncio
async def test_calendar_upsert_service_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=400, body={"detail": "invalid_duration"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.calendar_upsert_service(
            project_id=11, actor="@op", actor_role="operator", internal_token="t"
        )
    assert info.value.detail == "invalid_duration"


@pytest.mark.asyncio
async def test_calendar_delete_service_uses_request_delete(monkeypatch):
    request = _http_method_mock(
        monkeypatch, method="request", response_json={"deleted": True}
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.calendar_delete_service(
        project_id=11,
        rule_id=3,
        actor="@op",
        actor_role="operator",
        internal_token="svc",
    )
    assert result == {"deleted": True}
    args = request.await_args
    assert args.args[0] == "DELETE"
    assert args.args[1] == "http://api:8000/calendar/projects/11/services/3"
    assert args.kwargs["json"] == {"actor": "@op", "actor_role": "operator"}


@pytest.mark.asyncio
async def test_calendar_delete_service_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        method="request",
        response=_error_response(status_code=403, body={"detail": "x"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError):
        await client.calendar_delete_service(
            project_id=11,
            rule_id=3,
            actor="@op",
            actor_role="admin",
            internal_token="t",
        )


@pytest.mark.asyncio
async def test_set_persona_includes_optional_description_fields(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"first_name": "x", "last_name": "y"})
    client = ApiClient(base_url="http://api:8000")
    await client.set_persona(
        first_name="Иван",
        last_name="Сидоров",
        updated_by="@ajdevy",
        description="Здравствуйте.",
        short_description="На связи.",
    )
    assert http.post.await_args.kwargs["json"] == {
        "first_name": "Иван",
        "last_name": "Сидоров",
        "updated_by": "@ajdevy",
        "description": "Здравствуйте.",
        "short_description": "На связи.",
    }


def _http_delete_mock(monkeypatch, *, status_code: int, response_json: dict | None):
    response = Mock()
    response.status_code = status_code
    response.json.return_value = response_json
    response.raise_for_status = Mock()

    http_client = AsyncMock()
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
async def test_delete_operator_file_passes_bearer_and_as_user(monkeypatch):
    http = _http_delete_mock(
        monkeypatch,
        status_code=200,
        response_json={
            "deleted_files": 1,
            "deleted_chunks": 2,
            "deleted_candidates": 1,
            "deleted_binaries": 1,
            "failed_binary_paths": [],
        },
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.delete_operator_file(
        short_id="ABC1",
        requester_username="@alice",
        internal_token="bot-token",
    )
    assert result is not None
    assert result["deleted_files"] == 1
    args = http.delete.await_args
    assert args.args[0] == "http://api:8000/admin/files/ABC1"
    assert args.kwargs["params"] == {"as_user": "@alice"}
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-token"}


@pytest.mark.asyncio
async def test_delete_operator_file_returns_none_on_404(monkeypatch):
    _http_delete_mock(monkeypatch, status_code=404, response_json=None)
    client = ApiClient(base_url="http://api:8000")
    result = await client.delete_operator_file(
        short_id="GONE", requester_username="@alice", internal_token="t"
    )
    assert result is None


@pytest.mark.asyncio
async def test_delete_operator_file_raises_api_error_on_other_status(monkeypatch):
    response = _error_response(status_code=500, body={"detail": "boom"})
    _http_error_mock(monkeypatch, response=response, method="delete")
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.delete_operator_file(
            short_id="X", requester_username="@alice", internal_token="t"
        )
    assert info.value.detail == "boom"


@pytest.mark.asyncio
async def test_delete_all_operator_files_sends_confirm_query(monkeypatch):
    http = _http_delete_mock(
        monkeypatch,
        status_code=200,
        response_json={
            "deleted_files": 0,
            "deleted_chunks": 0,
            "deleted_candidates": 0,
            "deleted_binaries": 0,
            "failed_binary_paths": [],
        },
    )
    client = ApiClient(base_url="http://api:8000")
    await client.delete_all_operator_files(
        requester_username="@alice", internal_token="bot-token"
    )
    args = http.delete.await_args
    assert args.args[0] == "http://api:8000/admin/files"
    assert args.kwargs["params"] == {
        "as_user": "@alice",
        "confirm": "true",
    }
    assert args.kwargs["headers"] == {"Authorization": "Bearer bot-token"}


@pytest.mark.asyncio
async def test_delete_all_operator_files_raises_api_error(monkeypatch):
    response = _error_response(status_code=400, body={"detail": "confirm_required"})
    _http_error_mock(monkeypatch, response=response, method="delete")
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.delete_all_operator_files(
            requester_username="@alice", internal_token="t"
        )
    assert info.value.detail == "confirm_required"


# --- Story 13.03 canonical /api/projects/{id}/services methods ------------


@pytest.mark.asyncio
async def test_upsert_project_service_posts_with_actor_and_payload(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"id": 7, "name": "маникюр"})
    client = ApiClient(base_url="http://api:8000")
    result = await client.upsert_project_service(
        project_id=11,
        payload={
            "name": "маникюр",
            "duration_minutes": 60,
            "service_days": ["mon"],
        },
        actor="@op",
        actor_role="operator",
        internal_token="svc",
    )
    assert result == {"id": 7, "name": "маникюр"}
    args = http.post.await_args
    assert args.args[0] == "http://api:8000/api/projects/11/services"
    body = args.kwargs["json"]
    assert body["actor"] == "@op"
    assert body["actor_role"] == "operator"
    assert body["name"] == "маникюр"
    assert body["duration_minutes"] == 60
    assert body["service_days"] == ["mon"]
    assert args.kwargs["headers"] == {"Authorization": "Bearer svc"}


@pytest.mark.asyncio
async def test_upsert_project_service_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        response=_error_response(status_code=400, body={"detail": "invalid_duration"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.upsert_project_service(
            project_id=11,
            payload={"name": "x", "duration_minutes": -1},
            actor="@op",
            actor_role="operator",
            internal_token="t",
        )
    assert info.value.detail == "invalid_duration"


@pytest.mark.asyncio
async def test_list_project_services_uses_get(monkeypatch):
    http = _http_get_mock(
        monkeypatch,
        status_code=200,
        response_json={"project_id": 11, "services": []},
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.list_project_services(project_id=11, internal_token="svc")
    assert result == {"project_id": 11, "services": []}
    args = http.get.await_args
    assert args.args[0] == "http://api:8000/api/projects/11/services"
    assert args.kwargs["headers"] == {"Authorization": "Bearer svc"}


@pytest.mark.asyncio
async def test_list_project_services_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        method="get",
        response=_error_response(status_code=401, body={"detail": "missing"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.list_project_services(project_id=11, internal_token="t")
    assert info.value.detail == "missing"


@pytest.mark.asyncio
async def test_delete_project_service_uses_request_delete(monkeypatch):
    request = _http_method_mock(
        monkeypatch, method="request", response_json={"deleted": True}
    )
    client = ApiClient(base_url="http://api:8000")
    result = await client.delete_project_service(
        project_id=11,
        service_id=42,
        actor="@op",
        actor_role="operator",
        internal_token="svc",
    )
    assert result == {"deleted": True}
    args = request.await_args
    assert args.args[0] == "DELETE"
    assert args.args[1] == "http://api:8000/api/projects/11/services/42"
    assert args.kwargs["json"] == {"actor": "@op", "actor_role": "operator"}
    assert args.kwargs["headers"] == {"Authorization": "Bearer svc"}


@pytest.mark.asyncio
async def test_delete_project_service_raises_api_error(monkeypatch):
    _http_error_mock(
        monkeypatch,
        method="request",
        response=_error_response(
            status_code=403, body={"detail": "admin_cannot_remove_service"}
        ),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError) as info:
        await client.delete_project_service(
            project_id=11,
            service_id=42,
            actor="@op",
            actor_role="admin",
            internal_token="t",
        )
    assert info.value.detail == "admin_cannot_remove_service"


@pytest.mark.asyncio
async def test_operator_registration_methods_post_expected_paths(monkeypatch):
    http = _http_mock(monkeypatch, response_json={"ok": True})
    client = ApiClient(base_url="http://api:8000", internal_token="svc")

    await client.create_operator_register_request(
        username="@op",
        chat_id=101,
        display_name="Оператор",
    )
    assert http.post.await_args.args[0] == "http://api:8000/operators/register-request"
    assert http.post.await_args.kwargs["headers"] == {"X-Internal-Token": "svc"}

    await client.approve_operator_register_request(request_id=7, project_id=11)
    assert (
        http.post.await_args.args[0]
        == "http://api:8000/operators/register-requests/7/approve"
    )

    await client.reject_operator_register_request(request_id=7)
    assert (
        http.post.await_args.args[0]
        == "http://api:8000/operators/register-requests/7/reject"
    )

    await client.record_onboarding_event(
        operator_id=3, event_type="telegram_link_started"
    )
    assert (
        http.post.await_args.args[0]
        == "http://api:8000/operators/3/onboarding-events"
    )


@pytest.mark.asyncio
async def test_get_operator_by_id_returns_none_on_404(monkeypatch):
    _http_error_mock(
        monkeypatch,
        method="get",
        response=_error_response(status_code=404, body={"detail": "operator_not_found"}),
    )
    client = ApiClient(base_url="http://api:8000")
    assert await client.get_operator_by_id(operator_id=99) is None


@pytest.mark.asyncio
async def test_get_operator_by_id_reraises_non_404(monkeypatch):
    _http_error_mock(
        monkeypatch,
        method="get",
        response=_error_response(status_code=500, body={"detail": "server_error"}),
    )
    client = ApiClient(base_url="http://api:8000")
    with pytest.raises(ApiError):
        await client.get_operator_by_id(operator_id=99)
