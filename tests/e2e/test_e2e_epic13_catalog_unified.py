"""Epic 13 Story 13.06 — unified catalog answer via merge-with-dedup.

Boots the api service against a fresh per-test set of SQLite files,
stubs out the LLM (verifier always GROUNDED; ``answer_grounded`` echoes
the chunk text so we can grep the rendered prose) and verifies the
four FR-25 acceptance criteria:

(a) brownfield: empty ``project_services`` + non-empty digest →
    ``source_id`` is ``catalog_digest:<id>``.
(b) structured-only: 3 rows + empty digest → ``project_services:<id>``;
    NO field labels in the customer-visible answer.
(c) merged: 3 structured rows + a digest mentioning extra services →
    ``merged:<id>`` and all names surface.
(d) single-service question is bounded: only the asked service's prose
    reaches the LLM input (because the LLM is the only thing that can
    over-share — but we assert on the chunk_text the answerer feeds it).
"""

from __future__ import annotations

import hashlib
import sqlite3
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answer_trace import AnswerTraceRepository
from services.api.app.answerers.grounded_rag import GroundedRagAnswerer
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
from services.api.app.catalog_digest import (
    CatalogDigestRepository,
    CatalogDigestService,
)
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture

pytestmark = [pytest.mark.e2e, pytest.mark.epic("13"), pytest.mark.story("13-06")]


_FORBIDDEN_LABELS = (
    "Название:",
    "Описание:",
    "Цена:",
    "Длительность:",
    "Дни:",
    "Часы:",
)


def _compute_revision_key(chunk_hashes: list[str]) -> str:
    """Mirror ``CatalogDigestRepository.read_source``'s SHA-256 derivation."""
    fingerprint = "\n".join(sorted(chunk_hashes))
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def _seed_rag_chunk_and_digest(
    *,
    rag_repo,
    digest_repo: CatalogDigestRepository,
    project_id: int | None,
    digest_text: str,
) -> None:
    """Ingest a seed chunk via the real repo, then upsert a matching digest.

    Using the real ``RagRepository.ingest`` ensures the chunk_hash schema
    matches what ``read_source`` reads. We then compute the revision_key
    the same way ``read_source`` does so ``get_digest`` short-circuits
    instead of re-invoking the LLM.
    """
    rag_repo.ingest(
        source_id=f"seed:{project_id}",
        text=f"seed for project {project_id}",
        project_id=project_id,
    )
    # Pull every (non-confidential) chunk_hash visible to this project_id —
    # matches the scope used by ``read_source``.
    with sqlite3.connect(rag_repo.db_path) as conn:
        if project_id is None:
            rows = conn.execute(
                "SELECT chunk_hash FROM rag_chunks WHERE is_confidential = 0"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT chunk_hash FROM rag_chunks WHERE is_confidential = 0 "
                "AND (project_id = ? OR project_id IS NULL)",
                (project_id,),
            ).fetchall()
    revision_key = _compute_revision_key([str(r[0]) for r in rows])
    digest_repo.upsert(
        project_id=project_id,
        digest_text=digest_text,
        revision_key=revision_key,
    )


def _build_client(tmp_path, monkeypatch, *, llm_answer: str = ""):
    """Rebind every db_path to tmp_path and stub the LLM. Returns TestClient."""
    services_db = str(tmp_path / "services.sqlite3")
    rag_db = str(tmp_path / "rag.sqlite3")
    prompts_db = str(tmp_path / "prompts.sqlite3")
    trace_db = str(tmp_path / "traces.sqlite3")
    incidents_db = str(tmp_path / "incidents.sqlite3")
    hitl_db = str(tmp_path / "hitl.sqlite3")

    services_repo = ProjectServiceRepository(db_path=services_db)
    digest_repo = CatalogDigestRepository(rag_db)
    digest_service = CatalogDigestService(
        repository=digest_repo,
        openrouter_client=api_main.openrouter_client,
        project_prompt_repository=api_main.project_prompt_repository,
    )

    # Stub the LLM: verifier always GROUNDED; answer is the captured argument
    # so the test can introspect what the answerer fed the model.
    captured: dict[str, object] = {}

    _cap_answer = LlmUsageCapture(
        model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
        cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
    )
    _cap_verify = LlmUsageCapture(
        model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
        cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
    )

    async def _fake_answer(**kwargs):
        captured["snippets"] = kwargs.get("snippets")
        if llm_answer:
            return llm_answer, _cap_answer
        # Default: echo the first snippet's chunk_text so we can grep it.
        snippets = kwargs.get("snippets") or []
        return (snippets[0].chunk_text if snippets else ""), _cap_answer

    async def _fake_verify(**kwargs):
        return GroundingVerdict(label="GROUNDED", reason="echo-test-ok"), _cap_verify

    monkeypatch.setattr(
        api_main.openrouter_client,
        "answer_grounded",
        AsyncMock(side_effect=_fake_answer),
    )
    monkeypatch.setattr(
        api_main.openrouter_client,
        "verify_grounding",
        AsyncMock(side_effect=_fake_verify),
    )

    # Rebind repositories on the main module so the running app picks them up.
    monkeypatch.setattr(api_main, "project_services_repository", services_repo)
    monkeypatch.setattr(api_main, "catalog_digest_service", digest_service)
    # ``db_path`` is a mutable attribute on a shared repository instance —
    # use monkeypatch so it's restored at teardown (otherwise later tests
    # write to our tmp_path and break test isolation).
    monkeypatch.setattr(api_main.rag_repository, "db_path", rag_db)
    from services.api.app.rag import init_schema as _rag_init_schema

    _rag_init_schema(rag_db)
    monkeypatch.setattr(api_main.incident_repository, "db_path", incidents_db)
    monkeypatch.setattr(api_main.hitl_ticket_repository, "db_path", hitl_db)
    fresh_trace_repo = AnswerTraceRepository(
        db_path=trace_db,
        snippet_max_chars=api_main.settings.answer_trace_snippet_max_chars,
    )
    monkeypatch.setattr(api_main, "answer_trace_repository", fresh_trace_repo)
    monkeypatch.setattr(
        api_main.project_prompt_repository, "db_path", prompts_db
    )
    api_main.project_prompt_repository.init_schema()

    # Replace the GroundedRagAnswerer in the pipeline so it uses our rebound
    # collaborators. CalendarAvailabilityAnswerer stays in place (disabled by
    # default for fresh projects, so it's a cheap no-op skip).
    new_answerer = GroundedRagAnswerer(
        rag_repository=api_main.rag_repository,
        openrouter_client=api_main.openrouter_client,
        persona_reader=api_main._effective_bot_persona,
        project_prompt_repository=api_main.project_prompt_repository,
        catalog_digest_service=digest_service,
        weather_client=api_main.weather_client,
        project_services_reader=services_repo,
    )
    # Replace through monkeypatch.setattr on the list so test teardown restores
    # the original answerer — avoids polluting later tests that import
    # ``api_main.answer_pipeline`` at module scope. Swap the GroundedRagAnswerer
    # by TYPE rather than by position: the full pipeline ends with the
    # ScopeGuardAnswerer, so ``swapped[-1] = new_answerer`` would leave the
    # original GroundedRagAnswerer (bound to the real ``.data/`` repos) ahead of
    # ours and let leftover project_services rows for the default project leak
    # into the catalog answer.
    original_answerers = list(api_main.answer_pipeline._answerers)
    swapped = [
        new_answerer if isinstance(a, GroundedRagAnswerer) else a
        for a in original_answerers
    ]
    monkeypatch.setattr(api_main.answer_pipeline, "_answerers", swapped)

    return TestClient(api_main.app), captured, services_repo, digest_repo


def _post_inbound(client: TestClient, *, text: str, trace_id: str) -> dict:
    response = client.post(
        "/conversations/inbound",
        json={"text": text, "trace_id": trace_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_case_a_brownfield_digest_only(tmp_path, monkeypatch):
    client, captured, _, digest_repo = _build_client(tmp_path, monkeypatch)
    project_id = api_main._default_project_id()
    _seed_rag_chunk_and_digest(
        rag_repo=api_main.rag_repository,
        digest_repo=digest_repo,
        project_id=project_id,
        digest_text="- Багги-туры\n- Морские прогулки",
    )
    body = _post_inbound(client, text="какие услуги у вас есть?", trace_id="e2e-a")
    snippets = captured["snippets"]
    # No structured rows for this project + non-empty digest → digest-only path.
    assert snippets[0].source_id == f"catalog_digest:{project_id}"
    answer = body["answer_text"]
    for label in _FORBIDDEN_LABELS:
        assert label not in answer
    assert "Багги-туры" in answer


def test_case_b_structured_only_no_field_labels(tmp_path, monkeypatch):
    client, captured, services_repo, _ = _build_client(tmp_path, monkeypatch)
    project_id = api_main._default_project_id()
    assert project_id is not None
    services_repo.upsert(
        project_id=project_id,
        name="Маникюр",
        description="Классический",
        price_text="от 2000 ₽",
    )
    services_repo.upsert(
        project_id=project_id,
        name="Педикюр",
        price_text="от 2500 ₽",
    )
    services_repo.upsert(
        project_id=project_id, name="Стрижка"
    )
    # No digest seeded → catalog_digest_service.get_digest returns ""
    body = _post_inbound(client, text="какие услуги у вас есть?", trace_id="e2e-b")
    snippets = captured["snippets"]
    assert snippets[0].source_id == f"project_services:{project_id}"
    answer = body["answer_text"]
    for label in _FORBIDDEN_LABELS:
        assert label not in answer
    for name in ("Маникюр", "Педикюр", "Стрижка"):
        assert name in answer


def test_case_c_merged_with_overlap(tmp_path, monkeypatch):
    client, captured, services_repo, digest_repo = _build_client(
        tmp_path, monkeypatch
    )
    project_id = api_main._default_project_id()
    assert project_id is not None
    services_repo.upsert(
        project_id=project_id, name="Маникюр", price_text="от 2000 ₽"
    )
    services_repo.upsert(
        project_id=project_id, name="Педикюр", price_text="от 2500 ₽"
    )
    services_repo.upsert(project_id=project_id, name="Стрижка")
    _seed_rag_chunk_and_digest(
        rag_repo=api_main.rag_repository,
        digest_repo=digest_repo,
        project_id=project_id,
        digest_text="- Маникюр\n- Окрашивание\n- Прическа",
    )
    body = _post_inbound(client, text="какие услуги у вас есть?", trace_id="e2e-c")
    snippets = captured["snippets"]
    assert snippets[0].source_id == f"merged:{project_id}"
    answer = body["answer_text"]
    for label in _FORBIDDEN_LABELS:
        assert label not in answer
    # Structured + non-overlapping digest entries both surface.
    for name in ("Маникюр", "Педикюр", "Стрижка", "Окрашивание", "Прическа"):
        assert name in answer


def test_case_d_single_service_question_bounded(tmp_path, monkeypatch):
    """The non-catalog branch handles "сколько стоит X?" via RAG retrieval.

    We seed only the project_services prose for "Маникюр" into RAG so the
    answerer's lemma-overlap retrieval finds it and the LLM-echo verifies
    that no other services' price/description bleed into the input.
    """
    client, captured, services_repo, _ = _build_client(tmp_path, monkeypatch)
    project_id = api_main._default_project_id()
    services_repo.upsert(
        project_id=project_id,
        name="Маникюр",
        description="Классический",
        price_text="от 2000 ₽",
    )
    services_repo.upsert(
        project_id=project_id,
        name="Педикюр",
        description="Только женский",
        price_text="от 2500 ₽",
    )
    # Seed RAG with a single Маникюр line so the non-catalog branch finds it
    # and the LLM input contains only that row.
    api_main.rag_repository.ingest(
        source_id="manicure-info", text="Маникюр стоит от 2000 рублей."
    )
    monkeypatch.setattr(
        api_main.hitl_ticket_repository,
        "get_runtime_config",
        lambda key: None,
    )
    monkeypatch.setattr(
        api_main, "_effective_grounding_threshold", lambda: 0.0
    )
    body = _post_inbound(
        client, text="сколько стоит маникюр?", trace_id="e2e-d"
    )
    answer = body["answer_text"]
    # The seeded RAG chunk surfaces; the other service's price doesn't.
    assert "2000" in answer
    assert "2500" not in answer
    assert "Педикюр" not in answer
