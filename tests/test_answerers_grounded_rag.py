from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.answerers.grounded_rag import (
    GroundedRagAnswerer,
    _render_catalog_fallback,
)
from services.api.app.openrouter_client import GroundingVerdict
from services.api.app.project_prompts import ProjectPromptRepository
from services.api.app.rag import RagChunk


@pytest.fixture
def prompts(tmp_path) -> ProjectPromptRepository:
    return ProjectPromptRepository(str(tmp_path / "prompts.sqlite3"))


def _ctx(*, threshold: float = 0.6) -> AnswerContext:
    return AnswerContext(
        chat_id=1,
        customer_username="@c",
        trace_id="t-1",
        now=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
        grounding_threshold=threshold,
    )


def _chunks(score: float) -> list[RagChunk]:
    return [
        RagChunk(
            id=1, source_id="kb-1",
            chunk_text="Возврат денег занимает пять рабочих дней",
            score=score,
        )
    ]


class _FakeRag:
    def __init__(self, items: list[RagChunk]) -> None:
        self._items = items
        self.calls: list[str] = []
        self.last_limit: int | None = None

    def retrieve(
        self,
        *,
        query: str,
        limit: int = 3,
        project_id: int | None = None,
    ) -> list[RagChunk]:
        self.calls.append(query)
        self.last_project_id = project_id
        self.last_limit = limit
        return list(self._items)


def _fake_llm(
    *,
    answer: str = "Ответ из сниппета.",
    verdict_label: str = "GROUNDED",
    verdict_reason: str = "matches snippet",
):
    from services.api.app.openrouter_client import LlmUsageCapture
    _cap = LlmUsageCapture(
        model_name="gpt-4o", prompt_tokens=10, completion_tokens=5,
        cost_usd=None, created_at="2026-06-11T00:00:00Z",
    )
    llm = AsyncMock()
    llm.answer_grounded = AsyncMock(return_value=(answer, _cap))
    llm.verify_grounding = AsyncMock(
        return_value=(GroundingVerdict(label=verdict_label, reason=verdict_reason), _cap)
    )
    return llm


class _FakeCatalogDigest:
    def __init__(self, digest: str = "") -> None:
        self._digest = digest
        self.calls: list[int | None] = []

    async def get_digest(self, *, project_id: int | None) -> str:
        self.calls.append(project_id)
        return self._digest


class _BrokenCatalogDigest:
    async def get_digest(self, *, project_id: int | None) -> str:
        raise RuntimeError("OpenRouter unavailable")


def _assert_skip_log(
    caplog: pytest.LogCaptureFixture,
    *,
    reason: str,
    question: str,
    retrieved_count: int,
    top_score: float | None,
) -> logging.LogRecord:
    records = [
        r
        for r in caplog.records
        if r.message == "grounded_rag_skipped"
        and getattr(r, "reason", None) == reason
    ]
    assert records, f"expected grounded_rag_skipped log with reason={reason}"
    record = records[-1]
    assert record.query == question
    assert record.threshold == 0.6
    assert record.retrieved_count == retrieved_count
    assert record.top_score == top_score
    assert record.trace_id == "t-1"
    assert record.chunk_confidential_flags == [False] * retrieved_count
    assert record.chunk_project_ids == [None] * retrieved_count
    return record


@pytest.mark.asyncio
async def test_strong_retrieval_grounded_verifier_delivers_answer(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm(
        answer="Возврат занимает пять рабочих дней."
    )
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(
        logging.INFO, logger="services.api.app.answerers.grounded_rag"
    ):
        result = await answerer.try_answer(
            question="когда придёт мой возврат?", ctx=_ctx()
        )
    assert result.handled is True
    assert result.response_mode == "grounded_rag"
    assert "пять рабочих дней" in result.text
    assert result.metadata["verifier"] == "matches snippet"
    assert result.metadata["retrieval"][0]["source_id"] == "kb-1"
    # Persona name must reach the LLM call so it speaks in-character.
    assert llm.answer_grounded.await_args.kwargs["persona_first_name"] == "Анна"
    assert llm.answer_grounded.await_args.kwargs["persona_last_name"] == "Иванова"
    # Each pipeline stage emits a structured event.
    events = [r.message for r in caplog.records]
    for expected in (
        "grounded_rag_pipeline_entry",
        "grounded_rag_llm_request",
        "grounded_rag_llm_response",
        "grounded_rag_verifier_result",
        "grounded_rag_guardrail_result",
        "grounded_rag_profanity_result",
        "grounded_rag_delivered",
    ):
        assert expected in events, f"missing event {expected}"
    entry = next(
        r for r in caplog.records if r.message == "grounded_rag_pipeline_entry"
    )
    assert entry.chunk_confidential_flags == [False]
    assert entry.chunk_project_ids == [None]
    assert entry.top_score == 0.9
    delivered = next(
        r for r in caplog.records if r.message == "grounded_rag_delivered"
    )
    assert delivered.retrieval_source_ids == ["kb-1"]
    assert delivered.guardrail_score == 0.95


@pytest.mark.asyncio
async def test_scheduling_intent_passes_context_to_llm_calls(prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm(answer="Доставим ваш заказ завтра в течение рабочего дня.")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
        weather_client=None,
    )
    result = await answerer.try_answer(
        question="можете доставить заказ завтра?", ctx=_ctx()
    )
    assert result.handled is True
    answer_ctx = llm.answer_grounded.await_args.kwargs["scheduling_context"]
    verify_ctx = llm.verify_grounding.await_args.kwargs["scheduling_context"]
    assert answer_ctx is not None
    assert "Справочный контекст для планирования" in answer_ctx
    assert verify_ctx == answer_ctx


@pytest.mark.asyncio
async def test_non_scheduling_question_passes_no_context(prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm()
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    result = await answerer.try_answer(
        question="когда придёт мой возврат?", ctx=_ctx()
    )
    assert result.handled is True
    assert llm.answer_grounded.await_args.kwargs["scheduling_context"] is None
    assert llm.verify_grounding.await_args.kwargs["scheduling_context"] is None


@pytest.mark.asyncio
async def test_weak_retrieval_falls_through(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.2))
    llm = _fake_llm()
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    llm.answer_grounded.assert_not_awaited()
    record = _assert_skip_log(
        caplog,
        reason="below_threshold",
        question="q",
        retrieved_count=1,
        top_score=0.2,
    )
    assert record.chunk_source_ids == ["kb-1"]


@pytest.mark.asyncio
async def test_empty_retrieval_falls_through(caplog, prompts):
    rag = _FakeRag([])
    llm = _fake_llm()
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    llm.answer_grounded.assert_not_awaited()
    _assert_skip_log(
        caplog,
        reason="no_chunks",
        question="q",
        retrieved_count=0,
        top_score=None,
    )


@pytest.mark.asyncio
async def test_sentinel_response_escalates(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm(answer="ESCALATE_TO_HUMAN")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    llm.verify_grounding.assert_not_awaited()
    _assert_skip_log(
        caplog,
        reason="escalate_sentinel",
        question="q",
        retrieved_count=1,
        top_score=0.9,
    )


@pytest.mark.asyncio
async def test_verifier_not_grounded_escalates(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm(verdict_label="NOT_GROUNDED", verdict_reason="hallucination")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    record = _assert_skip_log(
        caplog,
        reason="verifier_not_grounded",
        question="q",
        retrieved_count=1,
        top_score=0.9,
    )
    assert record.verdict_label == "NOT_GROUNDED"
    assert record.verdict_reason == "hallucination"


@pytest.mark.asyncio
async def test_guardrail_hedge_escalates_even_when_verifier_grounded(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    # Hedging phrase will trigger evaluate_suggestion -> low_confidence.
    llm = _fake_llm(answer="Я не знаю точного ответа.")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    record = _assert_skip_log(
        caplog,
        reason="guardrail_invalid",
        question="q",
        retrieved_count=1,
        top_score=0.9,
    )
    assert hasattr(record, "guardrail_score")
    assert isinstance(record.guardrail_failure_reasons, list)
    assert record.guardrail_failure_reasons, "expected at least one reason"
    # The guardrail result event also fires with the same reasons.
    guardrail_events = [
        r for r in caplog.records if r.message == "grounded_rag_guardrail_result"
    ]
    assert guardrail_events and guardrail_events[-1].valid is False
    assert guardrail_events[-1].failure_reasons == record.guardrail_failure_reasons


@pytest.mark.asyncio
async def test_profane_llm_output_escalates(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm(answer="Полный пиздец, ничего не работает у нас.")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    _assert_skip_log(
        caplog,
        reason="profanity_detected",
        question="q",
        retrieved_count=1,
        top_score=0.9,
    )


@pytest.mark.asyncio
async def test_llm_generator_exception_falls_through(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm()
    llm.answer_grounded = AsyncMock(side_effect=RuntimeError("boom"))
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    record = _assert_skip_log(
        caplog,
        reason="llm_generator_error",
        question="q",
        retrieved_count=1,
        top_score=0.9,
    )
    assert "boom" in record.error


@pytest.mark.asyncio
async def test_catalog_llm_failure_returns_bounded_rag_excerpt(prompts):
    """A simple catalog question still gets factual content during an LLM outage."""
    raw_chunks = [
        RagChunk(
            id=1,
            source_id="uploaded:services",
            chunk_text="Багги-туры\n• Квадроциклы\n• Дополнительные услуги: пикник",
            score=1.0,
        )
    ]
    rag = _FakeRag(raw_chunks)
    llm = _fake_llm()
    llm.answer_grounded = AsyncMock(side_effect=RuntimeError("402 Payment Required"))
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_BrokenCatalogDigest(),
    )

    result = await answerer.try_answer(question="Услуги", ctx=_ctx())

    assert result.handled is True
    assert result.response_mode == "grounded_rag_fallback"
    assert "Багги-туры" in result.text
    assert "Квадроциклы" in result.text
    assert "Дополнительные услуги" in result.text
    assert result.metadata["fallback_reason"] == "llm_generator_error"
    assert rag.calls == ["Услуги"]
    llm.verify_grounding.assert_not_awaited()


def test_catalog_fallback_filters_confidential_duplicates_and_bounds_items():
    long_item = "Очень подробное предложение " + ("слово " * 80)
    chunks = [
        RagChunk(
            id=1,
            source_id="secret",
            chunk_text="Секретная услуга",
            score=1.0,
            is_confidential=True,
        ),
        RagChunk(
            id=2,
            source_id="public",
            chunk_text=(
                "Альфа-услуга\nАльфа-услуга\n"
                + long_item
                + "\n"
                + "\n".join(f"Услуга {index}" for index in range(10))
            ),
            score=1.0,
        ),
    ]

    result = _render_catalog_fallback(chunks)

    assert result is not None
    assert "Секретная услуга" not in result
    assert result.count("Альфа-услуга") == 1
    assert "…" in result
    assert result.count("• ") == 8
    assert _render_catalog_fallback(chunks[:1]) is None


@pytest.mark.asyncio
async def test_llm_verifier_exception_falls_through(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm()
    llm.verify_grounding = AsyncMock(side_effect=RuntimeError("verify failed"))
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.answerers.grounded_rag"):
        result = await answerer.try_answer(question="q", ctx=_ctx())
    assert result.handled is False
    record = _assert_skip_log(
        caplog,
        reason="verifier_error",
        question="q",
        retrieved_count=1,
        top_score=0.9,
    )
    assert "verify failed" in record.error


@pytest.mark.asyncio
async def test_per_project_prompt_overrides_reach_llm_and_guardrails(
    caplog, prompts
):
    """When the project has overrides, GroundedRagAnswerer feeds them through
    to the LLM (system prompts) and to the guardrail/profanity checks."""
    rag = _FakeRag(_chunks(score=0.9))
    llm = _fake_llm(answer="свеженькое мнение.")
    prompts.set(
        project_id=7,
        prompt_name="grounding_system",
        value="custom-{name}-{today_iso}",
        edited_by="@admin",
    )
    prompts.set(
        project_id=7,
        prompt_name="verifier_system",
        value="custom verifier",
        edited_by="@admin",
    )
    prompts.set(
        project_id=7,
        prompt_name="guardrail_hedges",
        value="мнение",
        edited_by="@admin",
    )
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    ctx = AnswerContext(
        chat_id=1,
        customer_username="@c",
        trace_id="t-prompt",
        now=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
        grounding_threshold=0.6,
        project_id=7,
    )
    result = await answerer.try_answer(
        question="что вы думаете?", ctx=ctx
    )
    # Override propagated to OpenRouter calls.
    assert llm.answer_grounded.await_args.kwargs["system_prompt_template"] == (
        "custom-{name}-{today_iso}"
    )
    assert llm.verify_grounding.await_args.kwargs["system_prompt"] == (
        "custom verifier"
    )
    # The custom hedge "мнение" appears in the answer, so the guardrail
    # rejects what the verifier accepted — the answerer must fall through.
    assert result.handled is False


_CATALOG_QUERY = "какие ещё услуги есть"


@pytest.mark.asyncio
async def test_catalog_query_grounds_on_digest_and_lists_offerings(prompts):
    # Catalog queries ground on the digest, not lemma retrieval — so the answer
    # reflects the whole offerings set even with no lexical overlap.
    rag = _FakeRag([])
    llm = _fake_llm(answer="У нас есть багги-туры, морские прогулки и трансфер.")
    catalog = _FakeCatalogDigest(
        digest="- Багги-туры\n- Морские прогулки\n- Трансфер"
    )
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
    )
    result = await answerer.try_answer(question=_CATALOG_QUERY, ctx=_ctx())
    assert result.handled is True
    assert "багги-туры" in result.text
    # Digest was consulted for this project; lemma retrieval was bypassed.
    assert catalog.calls == [None]
    assert rag.calls == []
    # The digest is what the LLM grounded on.
    snippets = llm.answer_grounded.await_args.kwargs["snippets"]
    assert snippets[0].chunk_text == "- Багги-туры\n- Морские прогулки\n- Трансфер"


@pytest.mark.asyncio
async def test_catalog_query_empty_digest_escalates(caplog, prompts):
    rag = _FakeRag([])
    llm = _fake_llm()
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(digest=""),
    )
    with caplog.at_level(
        logging.INFO, logger="services.api.app.answerers.grounded_rag"
    ):
        result = await answerer.try_answer(question=_CATALOG_QUERY, ctx=_ctx())
    assert result.handled is False
    llm.answer_grounded.assert_not_awaited()
    _assert_skip_log(
        caplog,
        reason="catalog_empty",
        question=_CATALOG_QUERY,
        retrieved_count=0,
        top_score=None,
    )


@pytest.mark.asyncio
async def test_catalog_query_still_gated_by_verifier(caplog, prompts):
    rag = _FakeRag([])
    llm = _fake_llm(verdict_label="NOT_GROUNDED", verdict_reason="hallucination")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(digest="- Багги-туры"),
    )
    with caplog.at_level(
        logging.INFO, logger="services.api.app.answerers.grounded_rag"
    ):
        result = await answerer.try_answer(question=_CATALOG_QUERY, ctx=_ctx())
    assert result.handled is False
    _assert_skip_log(
        caplog,
        reason="verifier_not_grounded",
        question=_CATALOG_QUERY,
        retrieved_count=1,
        top_score=1.0,
    )


# --- Story 13.06: catalog merge-with-dedup branch ----------------------------


class _FakeProjectServicesReader:
    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.calls: list[int] = []

    def list_for_project(self, *, project_id: int):
        self.calls.append(project_id)
        return list(self._rows)


def _svc(
    *,
    id: int,
    name: str,
    description: str | None = None,
    price_text: str | None = None,
    duration_minutes: int | None = None,
):
    from services.api.app.calendar.project_services_repository import ProjectService

    return ProjectService(
        id=id,
        project_id=1,
        name=name,
        description=description,
        price_text=price_text,
        tags=None,
        duration_minutes=duration_minutes,
        working_hours=None,
        service_days=None,
        date_exceptions=None,
        updated_at=None,
    )


def _ctx_with_project(project_id: int = 1) -> AnswerContext:
    return AnswerContext(
        chat_id=1,
        customer_username="@c",
        trace_id="t-1",
        now=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
        grounding_threshold=0.6,
        project_id=project_id,
    )


_FORBIDDEN_LABELS = (
    "Название:",
    "Описание:",
    "Цена:",
    "Длительность:",
    "Дни:",
    "Часы:",
)


@pytest.mark.asyncio
async def test_catalog_query_merges_structured_and_digest(prompts):
    rag = _FakeRag([])
    llm = _fake_llm(answer="У нас есть маникюр, педикюр, стрижка и трансфер.")
    reader = _FakeProjectServicesReader(
        [
            _svc(id=1, name="Маникюр", price_text="от 2000 ₽"),
            _svc(id=2, name="Педикюр"),
        ]
    )
    catalog = _FakeCatalogDigest(
        digest="- Маникюра у нас два вида\n- Стрижка\n- Трансфер"
    )
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
        project_services_reader=reader,
    )
    result = await answerer.try_answer(
        question=_CATALOG_QUERY, ctx=_ctx_with_project(1)
    )
    assert result.handled is True
    assert reader.calls == [1]
    snippets = llm.answer_grounded.await_args.kwargs["snippets"]
    assert len(snippets) == 1
    assert snippets[0].source_id == "merged:1"
    # Structured prose precedes the deduped digest.
    chunk_text = snippets[0].chunk_text
    assert "Маникюр" in chunk_text
    assert "Педикюр" in chunk_text
    assert "Стрижка" in chunk_text
    assert "Трансфер" in chunk_text
    # Lemma-matched digest line removed.
    assert "два вида" not in chunk_text
    for label in _FORBIDDEN_LABELS:
        assert label not in chunk_text
        assert label not in result.text


@pytest.mark.asyncio
async def test_catalog_query_structured_only_no_digest(prompts):
    rag = _FakeRag([])
    llm = _fake_llm(answer="Маникюр и педикюр.")
    reader = _FakeProjectServicesReader(
        [
            _svc(id=1, name="Маникюр"),
            _svc(id=2, name="Педикюр"),
        ]
    )
    catalog = _FakeCatalogDigest(digest="")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
        project_services_reader=reader,
    )
    result = await answerer.try_answer(
        question=_CATALOG_QUERY, ctx=_ctx_with_project(1)
    )
    assert result.handled is True
    snippets = llm.answer_grounded.await_args.kwargs["snippets"]
    assert snippets[0].source_id == "project_services:1"
    assert "Маникюр" in snippets[0].chunk_text
    assert "Педикюр" in snippets[0].chunk_text


@pytest.mark.asyncio
async def test_catalog_query_digest_only_when_structured_empty(prompts):
    rag = _FakeRag([])
    llm = _fake_llm(answer="У нас есть багги-туры и трансфер.")
    reader = _FakeProjectServicesReader([])
    catalog = _FakeCatalogDigest(digest="- Багги-туры\n- Трансфер")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
        project_services_reader=reader,
    )
    result = await answerer.try_answer(
        question=_CATALOG_QUERY, ctx=_ctx_with_project(1)
    )
    assert result.handled is True
    snippets = llm.answer_grounded.await_args.kwargs["snippets"]
    assert snippets[0].source_id == "catalog_digest:1"
    assert "Багги-туры" in snippets[0].chunk_text


@pytest.mark.asyncio
async def test_catalog_query_empty_structured_and_empty_digest_skips(caplog, prompts):
    rag = _FakeRag([])
    llm = _fake_llm()
    reader = _FakeProjectServicesReader([])
    catalog = _FakeCatalogDigest(digest="")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
        project_services_reader=reader,
    )
    with caplog.at_level(
        logging.INFO, logger="services.api.app.answerers.grounded_rag"
    ):
        result = await answerer.try_answer(
            question=_CATALOG_QUERY, ctx=_ctx_with_project(1)
        )
    assert result.handled is False
    llm.answer_grounded.assert_not_awaited()
    _assert_skip_log(
        caplog,
        reason="catalog_empty",
        question=_CATALOG_QUERY,
        retrieved_count=0,
        top_score=None,
    )


@pytest.mark.asyncio
async def test_catalog_query_without_project_id_skips_structured_read(prompts):
    """No project_id → structured reader is skipped; digest path still works."""
    rag = _FakeRag([])
    llm = _fake_llm(answer="У нас есть только трансфер.")
    reader = _FakeProjectServicesReader([_svc(id=1, name="Маникюр")])
    catalog = _FakeCatalogDigest(digest="- Трансфер")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
        project_services_reader=reader,
    )
    result = await answerer.try_answer(question=_CATALOG_QUERY, ctx=_ctx())
    assert result.handled is True
    # ctx.project_id is None, so the reader must NOT be called.
    assert reader.calls == []
    snippets = llm.answer_grounded.await_args.kwargs["snippets"]
    assert snippets[0].source_id == "catalog_digest:None"


@pytest.mark.asyncio
async def test_non_catalog_query_keeps_threshold_gate(caplog, prompts):
    rag = _FakeRag(_chunks(score=0.2))
    llm = _fake_llm()
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=prompts,
        catalog_digest_service=_FakeCatalogDigest(),
    )
    with caplog.at_level(
        logging.INFO, logger="services.api.app.answerers.grounded_rag"
    ):
        result = await answerer.try_answer(
            question="когда придёт мой возврат?", ctx=_ctx()
        )
    assert result.handled is False
    assert rag.last_limit == 3
    _assert_skip_log(
        caplog,
        reason="below_threshold",
        question="когда придёт мой возврат?",
        retrieved_count=1,
        top_score=0.2,
    )
