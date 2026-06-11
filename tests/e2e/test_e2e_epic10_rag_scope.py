"""Epic 10 story 10.06: RAG retrieval scoping by project_id end-to-end."""

from __future__ import annotations

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture
from services.api.app.project_prompts import ProjectPromptRepository
from services.api.app.projects import ProjectRepository
from services.api.app.rag import RagRepository

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10")]


_STUB_CAPTURE = LlmUsageCapture(
    model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
    cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
)
_STUB_VERIFY_CAPTURE = LlmUsageCapture(
    model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
    cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
)


class _StubLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.answer_calls: list[dict] = []

    async def answer_grounded(self, **kwargs):
        self.answer_calls.append(kwargs)
        return self.answer, _STUB_CAPTURE

    async def verify_grounding(self, **_kwargs):
        return GroundingVerdict(label="GROUNDED", reason="ok"), _STUB_VERIFY_CAPTURE


class _StubCatalog:
    async def get_digest(self, *, project_id: int | None) -> str:
        return ""


@pytest.mark.story("10-06")
@pytest.mark.asyncio
async def test_rag_scope_isolates_projects(tmp_path):
    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    rag = RagRepository(str(tmp_path / "rag.sqlite3"))
    a = projects.create(slug="a", name="A")
    b = projects.create(slug="b", name="B")
    rag.ingest(
        source_id="knowledge_candidate:1",
        text="Время работы для проекта A: 9-18.",
        project_id=a.id,
    )
    rag.ingest(
        source_id="knowledge_candidate:2",
        text="Время работы для проекта B: круглосуточно.",
        project_id=b.id,
    )
    llm = _StubLLM(answer="Время работы для проекта A: 9-18.")
    answerer = GroundedRagAnswerer(
        rag_repository=rag,
        openrouter_client=llm,  # type: ignore[arg-type]
        persona_reader=lambda: ("Анна", "Иванова"),
        project_prompt_repository=ProjectPromptRepository(
            str(tmp_path / "prompts.sqlite3")
        ),
        catalog_digest_service=_StubCatalog(),
    )

    from datetime import UTC, datetime

    ctx_a = AnswerContext(
        chat_id=1,
        customer_username="@c",
        trace_id="t",
        now=datetime.now(UTC),
        grounding_threshold=0.1,
        project_id=a.id,
    )
    result_a = await answerer.try_answer(
        question="время работы", ctx=ctx_a
    )
    assert result_a.handled
    sources_a = {item["source_id"] for item in result_a.metadata["retrieval"]}
    assert sources_a == {"knowledge_candidate:1"}

    ctx_b = AnswerContext(
        chat_id=1,
        customer_username="@c",
        trace_id="t",
        now=datetime.now(UTC),
        grounding_threshold=0.1,
        project_id=b.id,
    )
    llm.answer = "Время работы для проекта B: круглосуточно."
    result_b = await answerer.try_answer(
        question="время работы", ctx=ctx_b
    )
    assert result_b.handled
    sources_b = {item["source_id"] for item in result_b.metadata["retrieval"]}
    assert sources_b == {"knowledge_candidate:2"}


@pytest.mark.story("10-06")
def test_upload_scoping_via_resolution(tmp_path, monkeypatch):
    """OperatorUploadRequest precedence: explicit project_id > slug > operator > default."""
    from services.api.app import main as api_main
    from services.api.app.operators import OperatorRepository

    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    default = projects.ensure_default_project()
    billing = projects.create(slug="billing", name="Биллинг")
    operators.create(username="@op-a", project_id=billing.id)
    monkeypatch.setattr(api_main, "project_repository", projects)
    monkeypatch.setattr(api_main, "operator_repository", operators)

    # Explicit project_id wins.
    assert (
        api_main._resolve_upload_project_id(
            operator_username="@op-a", project_id=99, project_slug="billing"
        )
        == 99
    )
    # project_slug used when no explicit id.
    assert (
        api_main._resolve_upload_project_id(
            operator_username="@op-a", project_id=None, project_slug="billing"
        )
        == billing.id
    )
    # Fallback to operator's project_id when nothing else.
    assert (
        api_main._resolve_upload_project_id(
            operator_username="@op-a", project_id=None, project_slug=None
        )
        == billing.id
    )
    # Default for unknown operator.
    assert (
        api_main._resolve_upload_project_id(
            operator_username="@ghost", project_id=None, project_slug=None
        )
        == default.id
    )
    # Default also when project_slug doesn't exist.
    assert (
        api_main._resolve_upload_project_id(
            operator_username="@ghost", project_id=None, project_slug="ghost"
        )
        == default.id
    )
