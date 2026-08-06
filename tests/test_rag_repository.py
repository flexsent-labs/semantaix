import logging

import pytest

from services.api.app.rag import RagRepository, split_into_chunks


def test_split_into_chunks_removes_empty_lines():
    chunks = split_into_chunks("Hello\n\nWorld\n")
    assert chunks == ["Hello", "World"]


def test_split_into_chunks_returns_empty_for_blank_text():
    assert split_into_chunks(" \n \n") == []


def test_ingest_is_idempotent(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    first = repository.ingest(source_id="doc-1", text="line one\nline two")
    second = repository.ingest(source_id="doc-1", text="line one\nline two")
    assert first == 2
    assert second == 0


def test_retrieve_scores_and_limits(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-1",
        text="reset password via email link\nbilling cycle is monthly",
    )
    repository.ingest(source_id="kb-2", text="password reset requires account email")
    items = repository.retrieve(query="password reset", limit=1)
    assert len(items) == 1
    assert items[0].source_id in {"kb-1", "kb-2"}
    assert items[0].score > 0


def test_retrieve_matches_russian_inflection_via_lemma(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-ru",
        text="Возврат денег занимает пять рабочих дней",
    )
    items = repository.retrieve(query="когда придут деньги", limit=1)
    assert len(items) == 1
    assert items[0].source_id == "kb-ru"
    assert items[0].score > 0


def test_retrieve_matches_russian_slang_via_normalization(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-money",
        text="Возврат денег занимает пять рабочих дней",
    )
    # "бабло" should be slang-substituted to "деньги", then lemma-matched.
    items = repository.retrieve(query="когда придёт бабло", limit=1)
    assert len(items) == 1
    assert items[0].source_id == "kb-money"


def test_retrieve_buggy_tour_natural_language_query(tmp_path):
    """Regression: an intent-laden short query like "хочу поехать на багги тур"
    must score above the default grounding threshold (0.6) against a catalog
    chunk that mentions only the content nouns ("багги", "тур"). The intent /
    connector lemmas ("хотеть", "поехать", "на") are filtered from the scoring
    denominator so they don't deflate the overlap ratio.
    """
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-buggy",
        text="Багги-тур по дюнам. Ежедневно в 9:00. Стоимость 2500 руб.",
    )
    items = repository.retrieve(query="хочу поехать на багги тур", limit=1)
    assert len(items) == 1
    assert items[0].source_id == "kb-buggy"
    assert items[0].score >= 0.6
    assert items[0].score <= 1.0


def test_retrieve_score_uses_content_tokens_denominator(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-buggy",
        text="Багги-тур по дюнам. Стоимость 2500 руб.",
    )
    # Content-only query — both lemmas match the chunk, score must equal 1.0.
    items = repository.retrieve(query="багги тур", limit=1)
    assert len(items) == 1
    assert items[0].score == 1.0


def test_retrieve_stopword_only_query_falls_back(tmp_path):
    """A query made entirely of stop lemmas (e.g. "что? как?") must not award
    a perfect score against an arbitrary chunk. The scorer falls back to the
    full token set so the denominator stays non-zero and overlap stays partial.
    """
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-help",
        text="Возврат денег занимает пять рабочих дней",
    )
    items = repository.retrieve(query="что как где", limit=3)
    # Either no chunks (zero overlap) or low score — never a free 1.0.
    for item in items:
        assert item.score < 1.0


def test_retrieve_existential_copula_stripped_from_denominator(tmp_path):
    """Regression: "какие ещё услуги есть" must clear the 0.6 threshold against a
    chunk that mentions services. The copula "есть" (and interrogatives) are
    filtered from the denominator, leaving "услуга" as the only scoring token.
    """
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-svc",
        text="Наши услуги: багги-туры, морские прогулки, трансфер.",
    )
    items = repository.retrieve(query="какие ещё услуги есть", limit=3)
    assert len(items) == 1
    assert items[0].source_id == "kb-svc"
    assert items[0].score >= 0.6


def _records(caplog: pytest.LogCaptureFixture, event: str) -> list:
    return [r for r in caplog.records if r.message == event]


def test_retrieve_emits_request_and_result_events(tmp_path, caplog):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-buggy",
        text="Багги-тур по дюнам. Стоимость 2500 руб.",
    )
    with caplog.at_level(logging.INFO, logger="services.api.app.rag"):
        items = repository.retrieve(query="хочу поехать на багги тур", limit=3)
    assert items, "expected at least one chunk"
    request_records = _records(caplog, "rag_retrieve_request")
    assert request_records, "rag_retrieve_request not emitted"
    rec = request_records[-1]
    assert rec.query == "хочу поехать на багги тур"
    # pymorphy3 may pick a non-trivial canonical for "багги"; we don't
    # assert the exact surface form, only that content tokens exist and
    # stopwords are stripped.
    assert "тур" in rec.query_lemmas_content
    assert "хотеть" in rec.stopwords_removed
    assert "поехать" in rec.stopwords_removed
    assert "на" in rec.stopwords_removed
    assert rec.denominator == len(rec.query_lemmas_content)
    assert rec.project_id_filter is None
    assert rec.limit == 3
    result_records = _records(caplog, "rag_retrieve_result")
    assert result_records, "rag_retrieve_result not emitted"
    res = result_records[-1]
    assert res.returned_count == 1
    assert res.top_score == 1.0
    assert res.candidates[0]["source_id"] == "kb-buggy"
    assert "тур" in res.candidates[0]["matched_lemmas"]


def test_retrieve_empty_query_emits_empty_query_event(tmp_path, caplog):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(source_id="kb-1", text="anything")
    with caplog.at_level(logging.INFO, logger="services.api.app.rag"):
        items = repository.retrieve(query="...", limit=1)
    assert items == []
    assert _records(caplog, "rag_retrieve_empty_query"), \
        "empty-query event missing"
    assert _records(caplog, "rag_retrieve_request") == []


def test_retrieve_project_id_filter_scopes_results(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(
        source_id="kb-a", text="Багги-тур проект A", project_id=1,
    )
    repository.ingest(
        source_id="kb-b", text="Багги-тур проект B", project_id=2,
    )
    repository.ingest(
        source_id="kb-null", text="Багги-тур без проекта",
    )
    only_a = repository.retrieve(query="багги тур", limit=10, project_id=1)
    source_ids = {item.source_id for item in only_a}
    assert source_ids == {"kb-a", "kb-null"}
    only_b = repository.retrieve(query="багги тур", limit=10, project_id=2)
    assert {item.source_id for item in only_b} == {"kb-b", "kb-null"}
    unscoped = repository.retrieve(query="багги тур", limit=10)
    assert {item.source_id for item in unscoped} == {"kb-a", "kb-b", "kb-null"}


def test_list_for_catalog_returns_public_chunks_in_project_scope(tmp_path):
    repository = RagRepository(str(tmp_path / "rag.sqlite3"))
    repository.ingest(source_id="kb-a", text="Багги", project_id=1)
    repository.ingest(source_id="kb-global", text="Трансфер")
    repository.ingest(
        source_id="kb-private", text="Секрет", project_id=1, is_confidential=True
    )
    repository.ingest(source_id="kb-b", text="Квадроциклы", project_id=2)

    items = repository.list_for_catalog(project_id=1, limit=10)

    assert [item.source_id for item in items] == ["kb-a", "kb-global"]
    assert all(item.is_confidential is False for item in items)
    assert all(item.score == 1.0 for item in items)

    unscoped = repository.list_for_catalog(limit=10)
    assert [item.source_id for item in unscoped] == [
        "kb-a",
        "kb-global",
        "kb-b",
    ]
