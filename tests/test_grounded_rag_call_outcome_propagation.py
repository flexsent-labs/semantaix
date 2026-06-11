"""Tests for call_outcome propagation in GroundedRagAnswerer (Story 14.02).

Verifies that each pipeline exit path writes the correct call_outcome to the
UsageRecorder, and that the recorder is called with the right project_id and
both row counts (grounded + verifier where applicable).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture
from services.api.app.rag import RagChunk
from services.api.app.usage.recorder import UsageRecorder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GROUNDED_CAPTURE = LlmUsageCapture(
    model_name="gpt-4o", prompt_tokens=10, completion_tokens=20,
    cost_usd=0.003, created_at="2026-06-11T00:00:00Z",
)
_VERIFIER_CAPTURE = LlmUsageCapture(
    model_name="gpt-4o", prompt_tokens=5, completion_tokens=3,
    cost_usd=0.001, created_at="2026-06-11T00:00:01Z",
)


def _snippet() -> RagChunk:
    return RagChunk(id=1, source_id="doc:1", chunk_text="Доставка занимает 2 дня.", score=0.9)


_Q = "когда приедет курьер?"  # non-catalog question; triggers RAG path


def _ctx(project_id: int = 7) -> AnswerContext:
    from datetime import datetime, timezone
    return AnswerContext(
        chat_id=100,
        customer_username="user",
        trace_id="t-42",
        now=datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc),
        project_id=project_id,
    )


def _make_answerer(
    llm,
    recorder: UsageRecorder | None = None,
    rag_chunks: list[RagChunk] | None = None,
) -> GroundedRagAnswerer:
    rag = MagicMock()
    rag.retrieve = MagicMock(return_value=rag_chunks or [_snippet()])
    prompts = MagicMock()
    prompts.get_prompt = MagicMock(return_value=None)
    catalog = MagicMock()
    catalog.get_digest = AsyncMock(return_value="")

    return GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,
        persona_reader=lambda: ("Иван", "Иванов"),
        project_prompt_repository=prompts,
        catalog_digest_service=catalog,
        recorder=recorder,
    )


def _recorder_mock() -> UsageRecorder:
    rec = MagicMock(spec=UsageRecorder)
    rec.record = AsyncMock()
    return rec


def _fake_llm(
    answer: str = "Курьер приедет через 2 дня с момента оплаты.",
    verdict_label: str = "GROUNDED",
) -> MagicMock:
    llm = AsyncMock()
    llm.answer_grounded = AsyncMock(return_value=(answer, _GROUNDED_CAPTURE))
    llm.verify_grounding = AsyncMock(
        return_value=(
            GroundingVerdict(label=verdict_label, reason="ok"),
            _VERIFIER_CAPTURE,
        )
    )
    return llm


# ---------------------------------------------------------------------------
# Success path: both rows tagged customer_visible_answer
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_writes_customer_visible_answer():
    recorder = _recorder_mock()
    llm = _fake_llm()
    answerer = _make_answerer(llm, recorder=recorder)
    result = await answerer.try_answer(question=_Q, ctx=_ctx())
    assert result.handled
    await asyncio.sleep(0)  # flush fire-and-forget tasks
    calls = recorder.record.call_args_list
    outcomes = [c[1]["payload"]["call_outcome"] for c in calls]
    assert outcomes.count("customer_visible_answer") == 2


@pytest.mark.asyncio
async def test_success_writes_correct_project_id():
    recorder = _recorder_mock()
    llm = _fake_llm()
    answerer = _make_answerer(llm, recorder=recorder)
    await answerer.try_answer(question=_Q, ctx=_ctx(project_id=99))
    await asyncio.sleep(0)
    for c in recorder.record.call_args_list:
        assert c[1]["project_id"] == 99


# ---------------------------------------------------------------------------
# Verifier rejected path: both rows tagged verifier_rejected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verifier_rejected_tags_both_rows():
    recorder = _recorder_mock()
    llm = _fake_llm(verdict_label="NOT_GROUNDED")
    answerer = _make_answerer(llm, recorder=recorder)
    result = await answerer.try_answer(question=_Q, ctx=_ctx())
    assert not result.handled
    await asyncio.sleep(0)
    calls = recorder.record.call_args_list
    assert len(calls) == 2
    outcomes = [c[1]["payload"]["call_outcome"] for c in calls]
    assert all(o == "verifier_rejected" for o in outcomes)


# ---------------------------------------------------------------------------
# Guardrails blocked path: both rows tagged guardrails_blocked
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_guardrails_blocked_tags_both_rows():
    """evaluate_suggestion returns invalid → both rows tagged guardrails_blocked."""
    from unittest.mock import MagicMock, patch
    recorder = _recorder_mock()
    llm = _fake_llm(answer="Ответ.")
    answerer = _make_answerer(llm, recorder=recorder)
    blocked = MagicMock(valid=False, score=0.2, reasons=frozenset({"hedge"}))
    with patch(
        "services.api.app.answerers.grounded_rag.evaluate_suggestion",
        return_value=blocked,
    ):
        result = await answerer.try_answer(question=_Q, ctx=_ctx())
    assert not result.handled
    await asyncio.sleep(0)
    calls = recorder.record.call_args_list
    outcomes = [c[1]["payload"]["call_outcome"] for c in calls]
    assert len(calls) == 2
    assert all(o == "guardrails_blocked" for o in outcomes)


# ---------------------------------------------------------------------------
# Sentinel path: one row tagged escalated_to_hitl, no verifier call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sentinel_writes_escalated_to_hitl():
    recorder = _recorder_mock()
    llm = _fake_llm(answer="ESCALATE_TO_HUMAN")
    llm.verify_grounding = AsyncMock()  # must not be called
    answerer = _make_answerer(llm, recorder=recorder)
    result = await answerer.try_answer(question="что-то сложное?", ctx=_ctx())
    assert not result.handled
    await asyncio.sleep(0)
    calls = recorder.record.call_args_list
    assert len(calls) == 1
    assert calls[0][1]["payload"]["call_outcome"] == "escalated_to_hitl"
    llm.verify_grounding.assert_not_awaited()


# ---------------------------------------------------------------------------
# LLM error path: _chat already fires error row; answerer does NOT double-write
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_llm_error_answerer_does_not_record(caplog):
    """When answer_grounded raises, the answerer skips recording (recorder fires from _chat)."""
    recorder = _recorder_mock()
    llm = AsyncMock()
    llm.answer_grounded = AsyncMock(side_effect=RuntimeError("network down"))
    answerer = _make_answerer(llm, recorder=recorder)
    result = await answerer.try_answer(question=_Q, ctx=_ctx())
    assert not result.handled
    await asyncio.sleep(0)
    # answerer itself must NOT fire an extra row (the error row comes from _chat)
    recorder.record.assert_not_called()


# ---------------------------------------------------------------------------
# No recorder wired — no crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_recorder_does_not_crash():
    llm = _fake_llm()
    answerer = _make_answerer(llm, recorder=None)
    result = await answerer.try_answer(question=_Q, ctx=_ctx())
    assert result.handled


# ---------------------------------------------------------------------------
# No project_id — recorder silently skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_project_id_recorder_not_called():
    recorder = _recorder_mock()
    llm = _fake_llm()
    answerer = _make_answerer(llm, recorder=recorder)
    ctx_no_proj = AnswerContext(
        chat_id=1,
        customer_username=None,
        trace_id="t",
        now=__import__("datetime").datetime(2026, 6, 11),
        project_id=None,
    )
    await answerer.try_answer(question=_Q, ctx=ctx_no_proj)
    await asyncio.sleep(0)
    recorder.record.assert_not_called()
