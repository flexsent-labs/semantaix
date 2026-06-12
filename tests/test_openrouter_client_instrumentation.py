"""Tests for OpenRouterClient LLM usage capture (Story 14.02)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.api.app.openrouter_client import (
    LlmUsageCapture,
    OpenRouterClient,
)
from services.api.app.usage.recorder import UsageRecorder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_USAGE = {"prompt_tokens": 10, "completion_tokens": 20, "cost": 0.0034}

def _mock_response(
    text: str = "Hello",
    model: str = "gpt-4o",
    usage: dict | None = None,
) -> MagicMock:
    data = {
        "choices": [{"message": {"content": text}}],
        "model": model,
        "usage": usage if usage is not None else _USAGE,
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=data)
    return resp


def _make_client(recorder=None) -> OpenRouterClient:
    with patch("services.api.app.openrouter_client.get_settings") as ms:
        ms.return_value = MagicMock(
            openrouter_api_key="test-key",
            openrouter_base_url="https://or.test",
            openrouter_grounding_model="gpt-4o",
            openrouter_temperature=0.7,
        )
        client = OpenRouterClient(recorder=recorder)
    return client


# ---------------------------------------------------------------------------
# LlmUsageCapture dataclass
# ---------------------------------------------------------------------------

def test_llm_usage_capture_is_frozen():
    cap = LlmUsageCapture(
        model_name="gpt-4o",
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=0.003,
        created_at="2026-06-11T00:00:00Z",
    )
    with pytest.raises((AttributeError, TypeError)):
        cap.prompt_tokens = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _chat returns (text, LlmUsageCapture)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_returns_text_and_capture():
    client = _make_client()
    resp = _mock_response(text="hi", usage=_USAGE)
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        text, capture = await client._chat(model="gpt-4o", messages=[])
    assert text == "hi"
    assert isinstance(capture, LlmUsageCapture)
    assert capture.prompt_tokens == 10
    assert capture.completion_tokens == 20
    assert capture.cost_usd == 0.0034


@pytest.mark.asyncio
async def test_chat_capture_missing_cost_gives_none():
    client = _make_client()
    resp = _mock_response(usage={"prompt_tokens": 5, "completion_tokens": 8})
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        _, capture = await client._chat(model="gpt-4o", messages=[])
    assert capture.cost_usd is None


@pytest.mark.asyncio
async def test_chat_capture_model_name_from_response():
    client = _make_client()
    resp = _mock_response(model="anthropic/claude-3-sonnet")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        _, capture = await client._chat(model="gpt-4o", messages=[])
    assert capture.model_name == "anthropic/claude-3-sonnet"


# ---------------------------------------------------------------------------
# answer_grounded returns (str, LlmUsageCapture)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_answer_grounded_returns_text_and_capture():
    client = _make_client()
    resp = _mock_response(text="42 рубля")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        result = await client.answer_grounded(
            question="цена?",
            snippets=[],
            today_iso="2026-06-11",
            persona_first_name="Ivan",
            persona_last_name="Petrov",
        )
    text, capture = result
    assert text == "42 рубля"
    assert isinstance(capture, LlmUsageCapture)


# ---------------------------------------------------------------------------
# verify_grounding returns (GroundingVerdict, LlmUsageCapture)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_grounding_returns_verdict_and_capture():
    from services.api.app.openrouter_client import GroundingVerdict
    client = _make_client()
    resp = _mock_response(text="GROUNDED: all facts match snippets")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        result = await client.verify_grounding(
            question="цена?", answer="42 рубля", snippets=[]
        )
    verdict, capture = result
    assert isinstance(verdict, GroundingVerdict)
    assert verdict.label == "GROUNDED"
    assert isinstance(capture, LlmUsageCapture)


# ---------------------------------------------------------------------------
# Error path: error row fired before re-raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_http_error_fires_error_row_and_reraises():
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    client = _make_client(recorder=recorder)

    err_response = MagicMock()
    err_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    err_response.json = MagicMock(return_value={})

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=err_response)
        with pytest.raises(httpx.HTTPStatusError):
            await client._chat(
                model="gpt-4o",
                messages=[],
                project_id=1,
                call_outcome="customer_visible_answer",
                trace_id="t99",
            )

    # Give event loop a tick so the fire-and-forget coroutine runs
    import asyncio
    await asyncio.sleep(0)

    recorder.record.assert_called_once()
    call_kwargs = recorder.record.call_args[1]
    assert call_kwargs["tracker_type"] == "llm"
    assert call_kwargs["payload"]["call_outcome"] == "error"
    assert call_kwargs["payload"]["prompt_tokens"] == 0
    assert call_kwargs["payload"]["cost_usd"] is None


@pytest.mark.asyncio
async def test_http_error_without_recorder_does_not_crash():
    """When no recorder is wired, HTTP error still re-raises cleanly."""
    client = _make_client(recorder=None)
    err_response = MagicMock()
    err_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock()
    )
    err_response.json = MagicMock(return_value={})
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=err_response)
        with pytest.raises(httpx.HTTPStatusError):
            await client._chat(model="gpt-4o", messages=[])


# ---------------------------------------------------------------------------
# complete_json fires usage row immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_json_fires_usage_row():
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    client = _make_client(recorder=recorder)

    data = {
        "choices": [{"message": {"content": '{"sendable": true, "suggested_kind": "pdf"}'}}],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "cost": 0.001},
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=data)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        result = await client.complete_json(
            system="sys", user="usr", project_id=2,
            call_outcome="moderation_triggered", trace_id="t2",
        )

    import asyncio
    await asyncio.sleep(0)

    assert result == {"sendable": True, "suggested_kind": "pdf"}
    recorder.record.assert_called_once()
    kw = recorder.record.call_args[1]
    assert kw["tracker_type"] == "llm"
    assert kw["payload"]["call_outcome"] == "moderation_triggered"
    assert kw["payload"]["prompt_tokens"] == 5


@pytest.mark.asyncio
async def test_complete_json_http_error_fires_error_row():
    """complete_json fires an error row when the HTTP call raises."""
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    client = _make_client(recorder=recorder)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=httpx.RequestError("timeout")
        )
        with pytest.raises(httpx.RequestError):
            await client.complete_json(
                system="sys", user="usr", project_id=3,
                call_outcome="moderation_triggered", trace_id="t-err",
            )

    import asyncio
    await asyncio.sleep(0)

    recorder.record.assert_called_once()
    kw = recorder.record.call_args[1]
    assert kw["payload"]["call_outcome"] == "error"


@pytest.mark.asyncio
async def test_summarize_offerings_fires_usage_row():
    """summarize_offerings fires a usage row via ensure_future."""
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    client = _make_client(recorder=recorder)

    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=_mock_response("- Услуга А\n- Услуга Б")
        )
        text = await client.summarize_offerings(
            knowledge_text="Каталог услуг...",
            project_id=5,
            trace_id="t-so",
        )

    import asyncio
    await asyncio.sleep(0)

    assert "Услуга А" in text
    recorder.record.assert_called_once()
    kw = recorder.record.call_args[1]
    assert kw["tracker_type"] == "llm"
    assert kw["project_id"] == 5
    assert kw["payload"]["call_outcome"] == "customer_visible_answer"


@pytest.mark.asyncio
async def test_complete_json_parse_failure_fires_error_row():
    """complete_json fires an error row when json.loads raises (non-JSON 200 response)."""
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    client = _make_client(recorder=recorder)

    bad_data = {
        "choices": [{"message": {"content": "not json {{{"}}],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.0},
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=bad_data)

    from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        with pytest.raises(OpenRouterJsonSchemaViolation):
            await client.complete_json(
                system="sys", user="usr", project_id=4, trace_id="t-parse-err",
            )

    import asyncio
    await asyncio.sleep(0)

    recorder.record.assert_called_once()
    kw = recorder.record.call_args[1]
    assert kw["payload"]["call_outcome"] == "error"


@pytest.mark.asyncio
async def test_complete_json_non_dict_json_fires_error_row():
    """complete_json fires an error row when the response is valid JSON but not a dict."""
    recorder = MagicMock(spec=UsageRecorder)
    recorder.record = AsyncMock()
    client = _make_client(recorder=recorder)

    array_data = {
        "choices": [{"message": {"content": "[1, 2, 3]"}}],
        "model": "gpt-4o",
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.0},
    }
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=array_data)

    from services.api.app.openrouter_client import OpenRouterJsonSchemaViolation
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value.__aenter__.return_value.post = AsyncMock(return_value=resp)
        with pytest.raises(OpenRouterJsonSchemaViolation):
            await client.complete_json(
                system="sys", user="usr", project_id=5, trace_id="t-non-dict",
            )

    import asyncio
    await asyncio.sleep(0)

    recorder.record.assert_called_once()
    kw = recorder.record.call_args[1]
    assert kw["payload"]["call_outcome"] == "error"
