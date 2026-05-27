from unittest.mock import AsyncMock, Mock

import pytest

from services.api.app.openrouter_client import (
    GroundingVerdict,
    OpenRouterClient,
    OpenRouterJsonSchemaViolation,
    _parse_verdict,
)
from services.api.app.rag import RagChunk


def _http_mock(monkeypatch, *, content: str):
    response = Mock()
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    response.raise_for_status = Mock()

    http_client = AsyncMock()
    http_client.post.return_value = response

    async_client_cm = AsyncMock()
    async_client_cm.__aenter__.return_value = http_client
    async_client_cm.__aexit__.return_value = None

    monkeypatch.setattr(
        "services.api.app.openrouter_client.httpx.AsyncClient",
        lambda timeout: async_client_cm,
    )
    return http_client


def _snippet() -> RagChunk:
    return RagChunk(id=1, source_id="kb-1", chunk_text="text", score=0.9)


@pytest.mark.asyncio
async def test_answer_grounded_requires_api_key():
    client = OpenRouterClient()
    client.api_key = None
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await client.answer_grounded(
            question="hi",
            snippets=[_snippet()],
            today_iso="2026-05-11",
            persona_first_name="Анна",
            persona_last_name="Иванова",
        )


@pytest.mark.asyncio
async def test_answer_grounded_uses_grounding_model_and_sends_context(monkeypatch):
    http_client = _http_mock(monkeypatch, content="Final answer.")
    client = OpenRouterClient()
    client.api_key = "token"
    client.base_url = "https://openrouter.ai/api/v1"
    client.grounding_model = "google/gemini-2.0-flash-lite-001"

    result = await client.answer_grounded(
        question="Когда мой возврат?",
        snippets=[_snippet()],
        today_iso="2026-05-11",
        persona_first_name="Анна",
        persona_last_name="Иванова",
    )

    assert result == "Final answer."
    sent = http_client.post.call_args.kwargs["json"]
    assert sent["model"] == "google/gemini-2.0-flash-lite-001"
    assert sent["messages"][0]["role"] == "system"
    system_prompt = sent["messages"][0]["content"]
    assert "ESCALATE_TO_HUMAN" in system_prompt
    assert "2026-05-11" in system_prompt
    assert "Анна Иванова" in system_prompt
    # Persona must NOT identify the agent as a bot/assistant/AI.
    assert "Ты — ассистент" not in system_prompt
    assert "Когда мой возврат?" in sent["messages"][1]["content"]


@pytest.mark.asyncio
async def test_answer_grounded_system_prompt_suppresses_date_unless_asked(monkeypatch):
    http_client = _http_mock(monkeypatch, content="ok")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.answer_grounded(
        question="какие услуги есть",
        snippets=[_snippet()],
        today_iso="2026-05-11",
        persona_first_name="Анна",
        persona_last_name="Иванова",
    )
    system = http_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    # The date is reference-only: the model must not volunteer it in answers
    # unless the customer explicitly asks about timing.
    assert "не упоминай её в ответе" in system
    assert "если пользователь явно не" in system


@pytest.mark.asyncio
async def test_answer_grounded_appends_scheduling_context(monkeypatch):
    http_client = _http_mock(monkeypatch, content="ok")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.answer_grounded(
        question="можете доставить заказ?",
        snippets=[_snippet()],
        today_iso="2026-05-11",
        persona_first_name="Анна",
        persona_last_name="Иванова",
        scheduling_context="Справочный контекст: сегодня 11 мая.",
    )
    user_block = http_client.post.call_args.kwargs["json"]["messages"][1]["content"]
    assert "Справочный контекст: сегодня 11 мая." in user_block
    # System prompt now permits using scheduling context.
    system = http_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "контекст для планирования" in system.lower()


@pytest.mark.asyncio
async def test_verify_grounding_appends_scheduling_context(monkeypatch):
    http_client = _http_mock(monkeypatch, content="GROUNDED: ok.")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.verify_grounding(
        question="q",
        answer="a",
        snippets=[_snippet()],
        scheduling_context="Reference context: today is May 11.",
    )
    user_block = http_client.post.call_args.kwargs["json"]["messages"][1]["content"]
    assert "Reference context: today is May 11." in user_block
    assert "Candidate answer:\na" in user_block


@pytest.mark.asyncio
async def test_answer_grounded_respects_model_override(monkeypatch):
    http_client = _http_mock(monkeypatch, content="x")
    client = OpenRouterClient()
    client.api_key = "token"
    client.grounding_model = "default-model"

    await client.answer_grounded(
        question="q",
        snippets=[_snippet()],
        today_iso="2026-05-11",
        persona_first_name="Мария",
        persona_last_name="Петрова",
        model="override-model",
    )
    assert http_client.post.call_args.kwargs["json"]["model"] == "override-model"


@pytest.mark.asyncio
async def test_answer_grounded_injects_persona_name_into_system_prompt(monkeypatch):
    http_client = _http_mock(monkeypatch, content="ok")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.answer_grounded(
        question="q",
        snippets=[_snippet()],
        today_iso="2026-05-11",
        persona_first_name="Иван",
        persona_last_name="Сидоров",
    )
    system_prompt = http_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "Иван Сидоров" in system_prompt
    # The prompt must instruct the LLM not to self-identify as a bot.
    assert "не пиши, что ты бот" in system_prompt.lower()
    # And to speak as a first-person-plural employee of the company in the
    # snippets, never naming it in third person — locks in the voice rule
    # that prevents answers like "Компания X предлагает туры на багги".
    assert "от первого лица" in system_prompt.lower()
    assert "мы предлагаем" in system_prompt.lower()
    assert "запрещено" in system_prompt.lower()


@pytest.mark.asyncio
async def test_answer_grounded_strips_persona_when_last_name_is_empty(monkeypatch):
    """Last name is optional — the system prompt must render `Ты — Анна,` (no
    trailing space before the comma) so it reads naturally to the LLM."""
    http_client = _http_mock(monkeypatch, content="ok")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.answer_grounded(
        question="q",
        snippets=[_snippet()],
        today_iso="2026-05-11",
        persona_first_name="Анна",
        persona_last_name="",
    )
    system_prompt = http_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert "Ты — Анна, сотрудник" in system_prompt
    assert "Ты — Анна , сотрудник" not in system_prompt
    assert "Ты —  Анна" not in system_prompt


@pytest.mark.asyncio
async def test_verify_grounding_parses_grounded(monkeypatch):
    _http_mock(monkeypatch, content="GROUNDED: matches the snippet exactly.")
    client = OpenRouterClient()
    client.api_key = "token"
    verdict = await client.verify_grounding(
        question="q", answer="a", snippets=[_snippet()]
    )
    assert verdict.label == "GROUNDED"
    assert "matches" in verdict.reason


@pytest.mark.asyncio
async def test_verify_grounding_parses_not_grounded(monkeypatch):
    _http_mock(monkeypatch, content="NOT_GROUNDED: snippet does not cover that.")
    client = OpenRouterClient()
    client.api_key = "token"
    verdict = await client.verify_grounding(
        question="q", answer="a", snippets=[_snippet()]
    )
    assert verdict.label == "NOT_GROUNDED"
    assert "snippet" in verdict.reason


def test_parse_verdict_unparseable_defaults_to_not_grounded():
    verdict = _parse_verdict("model emitted prose, no verdict prefix")
    assert verdict.label == "NOT_GROUNDED"
    assert "unparseable" in verdict.reason


def test_grounding_verdict_dataclass_immutable():
    v = GroundingVerdict(label="GROUNDED", reason="ok")
    with pytest.raises(Exception):
        v.label = "NOT_GROUNDED"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_answer_grounded_uses_overridden_system_prompt_template(monkeypatch):
    http_client = _http_mock(monkeypatch, content="ok")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.answer_grounded(
        question="q",
        snippets=[_snippet()],
        today_iso="2026-05-20",
        persona_first_name="Анна",
        persona_last_name="Иванова",
        system_prompt_template="Custom template — {name}, день {today_iso}.",
    )
    system = http_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert system == "Custom template — Анна Иванова, день 2026-05-20."


@pytest.mark.asyncio
async def test_summarize_offerings_uses_default_prompt_and_grounding_model(monkeypatch):
    http_client = _http_mock(monkeypatch, content="- Тур на багги\n- Прокат лодок")
    client = OpenRouterClient()
    client.api_key = "token"
    client.grounding_model = "digest-model"

    result = await client.summarize_offerings(
        knowledge_text="Мы возим на багги. Сдаём лодки в аренду."
    )

    assert result == "- Тур на багги\n- Прокат лодок"
    sent = http_client.post.call_args.kwargs["json"]
    assert sent["model"] == "digest-model"
    system = sent["messages"][0]["content"]
    assert "NO_OFFERINGS" in system
    assert sent["messages"][1]["content"] == "Мы возим на багги. Сдаём лодки в аренду."


@pytest.mark.asyncio
async def test_summarize_offerings_respects_overrides(monkeypatch):
    http_client = _http_mock(monkeypatch, content="NO_OFFERINGS")
    client = OpenRouterClient()
    client.api_key = "token"

    result = await client.summarize_offerings(
        knowledge_text="Сегодня хорошая погода.",
        model="override-model",
        system_prompt="List offerings or NONE.",
    )

    assert result == "NO_OFFERINGS"
    sent = http_client.post.call_args.kwargs["json"]
    assert sent["model"] == "override-model"
    assert sent["messages"][0]["content"] == "List offerings or NONE."


@pytest.mark.asyncio
async def test_verify_grounding_uses_overridden_system_prompt(monkeypatch):
    http_client = _http_mock(monkeypatch, content="GROUNDED: ok.")
    client = OpenRouterClient()
    client.api_key = "token"
    await client.verify_grounding(
        question="q",
        answer="a",
        snippets=[_snippet()],
        system_prompt="Use only YES or NO.",
    )
    system = http_client.post.call_args.kwargs["json"]["messages"][0]["content"]
    assert system == "Use only YES or NO."


@pytest.mark.asyncio
async def test_complete_json_requires_api_key():
    client = OpenRouterClient()
    client.api_key = None
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await client.complete_json(system="sys", user="user")


@pytest.mark.asyncio
async def test_complete_json_returns_decoded_object(monkeypatch):
    http_client = _http_mock(
        monkeypatch,
        content='{"extracted_fields": {"dates": "1 мая"}, "next_question": "..."}',
    )
    client = OpenRouterClient()
    client.api_key = "token"
    client.grounding_model = "default-json-model"

    payload = await client.complete_json(system="sys", user="user msg")

    assert payload == {
        "extracted_fields": {"dates": "1 мая"},
        "next_question": "...",
    }
    sent = http_client.post.call_args.kwargs["json"]
    assert sent["model"] == "default-json-model"
    assert sent["response_format"] == {"type": "json_object"}
    assert sent["messages"][0] == {"role": "system", "content": "sys"}
    assert sent["messages"][1] == {"role": "user", "content": "user msg"}


@pytest.mark.asyncio
async def test_complete_json_model_override(monkeypatch):
    http_client = _http_mock(monkeypatch, content='{"a": 1}')
    client = OpenRouterClient()
    client.api_key = "token"
    await client.complete_json(
        system="sys", user="user", model="override-json-model"
    )
    assert (
        http_client.post.call_args.kwargs["json"]["model"]
        == "override-json-model"
    )


@pytest.mark.asyncio
async def test_complete_json_raises_on_non_json_response(monkeypatch):
    _http_mock(monkeypatch, content="not really json {[")
    client = OpenRouterClient()
    client.api_key = "token"
    with pytest.raises(OpenRouterJsonSchemaViolation):
        await client.complete_json(system="sys", user="user")


@pytest.mark.asyncio
async def test_complete_json_raises_on_non_object_root(monkeypatch):
    _http_mock(monkeypatch, content='["just", "a", "list"]')
    client = OpenRouterClient()
    client.api_key = "token"
    with pytest.raises(OpenRouterJsonSchemaViolation):
        await client.complete_json(system="sys", user="user")
