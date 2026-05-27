"""Tests for the KB-material lookup method on ``OperatorFilesView`` (12.05b).

The analyzer needs ``extracted_text`` plus the file metadata
(``mime_type``, ``file_extension``, ``byte_size``, ``local_path``,
``is_confidential``, ``project_id``) for a given operator-files
``short_id``. The new ``get_for_kb_material`` method exposes exactly
that view — server-internal, no viewer scope.
"""

from __future__ import annotations

from pathlib import Path

from services.api.app.knowledge_moderation import KnowledgeModerationRepository
from services.api.app.operator_files_view import OperatorFilesView
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_update import TelegramAttachment


def _attach(name: str, *, size: int, mime: str) -> TelegramAttachment:
    return TelegramAttachment(
        file_id="tg-" + name,
        kind="document",
        mime_type=mime,
        file_size=size,
        file_name=name,
    )


def _setup(tmp_path: Path) -> tuple[
    OperatorFilesView, OperatorFileRepository, KnowledgeModerationRepository
]:
    operator_files_db = tmp_path / "op_files.db"
    files_repo = OperatorFileRepository(db_path=str(operator_files_db))
    knowledge_db = tmp_path / "knowledge.db"
    moderation_repo = KnowledgeModerationRepository(db_path=str(knowledge_db))
    view = OperatorFilesView(
        operator_files_db_path=str(operator_files_db),
        knowledge_db_path=str(knowledge_db),
    )
    return view, files_repo, moderation_repo


def test_get_for_kb_material_returns_full_metadata(tmp_path: Path) -> None:
    view, files_repo, moderation_repo = _setup(tmp_path)
    moderation_row = moderation_repo.create_pending(
        text="Каталог туров на квадроциклах…"
    )
    moderation_repo.set_project_id(
        candidate_id=moderation_row.id, project_id=42
    )
    record = files_repo.record_upload(
        chat_id=99,
        username="@alice",
        source_message_id=1,
        attachment=_attach(
            "Каталог.PDF", size=8192, mime="application/pdf"
        ),
        is_confidential=False,
        stored_binary_path="/data/uploads/catalog.pdf",
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=4,
    )
    files_repo.set_candidate_id(
        short_id=record.short_id, knowledge_candidate_id=moderation_row.id
    )

    result = view.get_for_kb_material(short_id=record.short_id)

    assert result is not None
    assert result.short_id == record.short_id
    assert result.mime_type == "application/pdf"
    assert result.file_extension == "pdf"  # lowercased, no leading dot
    assert result.byte_size == 8192
    assert result.local_path == "/data/uploads/catalog.pdf"
    assert result.is_confidential is False
    assert result.extracted_text == "Каталог туров на квадроциклах…"
    assert result.project_id == 42


def test_get_for_kb_material_returns_none_for_unknown_short_id(
    tmp_path: Path,
) -> None:
    view, _, _ = _setup(tmp_path)
    assert view.get_for_kb_material(short_id="DOESNTEXIST") is None


def test_get_for_kb_material_marks_confidential_files(tmp_path: Path) -> None:
    view, files_repo, moderation_repo = _setup(tmp_path)
    moderation_row = moderation_repo.create_pending(
        text="confidential body"
    )
    record = files_repo.record_upload(
        chat_id=99,
        username="@alice",
        source_message_id=1,
        attachment=_attach("internal.pdf", size=100, mime="application/pdf"),
        is_confidential=True,
        stored_binary_path="/data/uploads/internal.pdf",
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=1,
    )
    files_repo.set_candidate_id(
        short_id=record.short_id, knowledge_candidate_id=moderation_row.id
    )

    result = view.get_for_kb_material(short_id=record.short_id)

    assert result is not None
    assert result.is_confidential is True


def test_get_for_kb_material_falls_back_to_source_type_when_no_dot_in_name(
    tmp_path: Path,
) -> None:
    """When ``source_file_name`` has no extension, fall back to the
    normalized ``source_file_type`` so the prompt still gets a hint.
    """
    view, files_repo, moderation_repo = _setup(tmp_path)
    moderation_row = moderation_repo.create_pending(text="hello")
    record = files_repo.record_upload(
        chat_id=99,
        username="@alice",
        source_message_id=1,
        attachment=TelegramAttachment(
            file_id="tg-x",
            kind="document",
            mime_type=None,
            file_size=100,
            file_name=None,
        ),
        is_confidential=False,
        stored_binary_path="/data/uploads/x.bin",
        download_status="ok",
        source_file_type="pdf",
        kb_ingest_status="ok",
        kb_inserted_chunks=1,
    )
    files_repo.set_candidate_id(
        short_id=record.short_id, knowledge_candidate_id=moderation_row.id
    )

    result = view.get_for_kb_material(short_id=record.short_id)

    assert result is not None
    assert result.file_extension == "pdf"


def test_resolve_file_extension_defaults_to_bin_when_no_hints() -> None:
    """Both ``source_file_name`` and ``source_file_type`` absent → ``"bin"``.

    Exercises the final fallback in ``_resolve_file_extension`` so the
    extension passed to the LLM is never empty.
    """
    from services.api.app.operator_files_view import _resolve_file_extension

    assert (
        _resolve_file_extension(
            source_file_name=None, source_file_type=None
        )
        == "bin"
    )


def test_get_for_kb_material_uses_jpg_for_image_type(tmp_path: Path) -> None:
    """A photo upload (kind=photo, no extension in name) → ``jpg``."""
    view, files_repo, moderation_repo = _setup(tmp_path)
    moderation_row = moderation_repo.create_pending(text="photo desc")
    record = files_repo.record_upload(
        chat_id=99,
        username="@alice",
        source_message_id=1,
        attachment=TelegramAttachment(
            file_id="tg-photo",
            kind="photo",
            mime_type="image/jpeg",
            file_size=100,
            file_name=None,
        ),
        is_confidential=False,
        stored_binary_path="/data/uploads/p.jpg",
        download_status="ok",
        source_file_type="image",
        kb_ingest_status="ok",
        kb_inserted_chunks=1,
    )
    files_repo.set_candidate_id(
        short_id=record.short_id, knowledge_candidate_id=moderation_row.id
    )

    result = view.get_for_kb_material(short_id=record.short_id)

    assert result is not None
    assert result.file_extension == "jpg"


def test_get_for_kb_material_returns_none_when_operator_files_db_missing(
    tmp_path: Path,
) -> None:
    """A KB-upload lookup before any operator has uploaded a file (i.e. before
    the operator_files SQLite DB has been created) must return ``None`` so the
    analyzer can record ``registered=False`` rather than crash the request.

    Regression for the epic-12 signoff Step 3/9 failure where this raised
    ``FileNotFoundError`` and surfaced as a 500 on
    ``POST /sales/materials/analyze-kb-file``.
    """
    view = OperatorFilesView(
        operator_files_db_path=str(tmp_path / "does_not_exist.db"),
        knowledge_db_path=str(tmp_path / "knowledge_also_missing.db"),
    )

    result = view.get_for_kb_material(short_id="anything")

    assert result is None
