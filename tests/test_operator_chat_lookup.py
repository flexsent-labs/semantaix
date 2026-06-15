from __future__ import annotations

from pathlib import Path

from services.api.app.operator_chat_lookup import resolve_chat_id_for_username
from services.api.app.operators import OperatorRepository
from services.bot_gateway.app.operator_files import OperatorFileRepository
from services.bot_gateway.app.telegram_update import TelegramAttachment


def _attachment(file_id: str = "f1") -> TelegramAttachment:
    return TelegramAttachment(
        file_id=file_id,
        kind="document",
        mime_type="application/pdf",
        file_size=10,
        file_name="x.pdf",
    )


def _seed_operator_file(repo: OperatorFileRepository, *, chat_id: int, username: str) -> None:
    repo.record_upload(
        chat_id=chat_id,
        username=username,
        source_message_id=1,
        attachment=_attachment(),
        is_confidential=False,
        stored_binary_path=None,
        download_status="ok",
        source_file_type="pdf",
    )


def test_resolves_from_latest_operator_files_row(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    repo = OperatorFileRepository(db_path=str(operator_files_db))
    _seed_operator_file(repo, chat_id=4242, username="@alice")
    result = resolve_chat_id_for_username(
        username="@alice",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=str(tmp_path / "operators.db"),
    )
    assert result == 4242


def test_returns_latest_chat_id_when_multiple_rows(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    repo = OperatorFileRepository(db_path=str(operator_files_db))
    _seed_operator_file(repo, chat_id=1, username="@alice")
    _seed_operator_file(repo, chat_id=2, username="@alice")
    _seed_operator_file(repo, chat_id=3, username="@alice")
    result = resolve_chat_id_for_username(
        username="@alice",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=str(tmp_path / "operators.db"),
    )
    assert result == 3


def test_falls_back_to_operators_table_for_registered_operator(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    OperatorFileRepository(db_path=str(operator_files_db))  # init only
    operators_db = str(tmp_path / "operators.db")
    op_repo = OperatorRepository(operators_db)
    op_repo.create(username="@ajdevy", project_id=1, chat_id=9999)
    result = resolve_chat_id_for_username(
        username="@ajdevy",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=operators_db,
    )
    assert result == 9999


def test_operators_table_not_used_when_no_chat_id(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    OperatorFileRepository(db_path=str(operator_files_db))
    operators_db = str(tmp_path / "operators.db")
    op_repo = OperatorRepository(operators_db)
    op_repo.create(username="@ajdevy", project_id=1, chat_id=None)
    result = resolve_chat_id_for_username(
        username="@ajdevy",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=operators_db,
    )
    assert result is None


def test_returns_none_when_no_signal(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    OperatorFileRepository(db_path=str(operator_files_db))
    result = resolve_chat_id_for_username(
        username="@alice",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=str(tmp_path / "operators.db"),
    )
    assert result is None


def test_operator_files_takes_precedence_over_operators_table(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    repo = OperatorFileRepository(db_path=str(operator_files_db))
    _seed_operator_file(repo, chat_id=555, username="@ajdevy")
    operators_db = str(tmp_path / "operators.db")
    op_repo = OperatorRepository(operators_db)
    op_repo.create(username="@ajdevy", project_id=1, chat_id=9999)
    result = resolve_chat_id_for_username(
        username="@ajdevy",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=operators_db,
    )
    assert result == 555


def test_returns_none_for_unknown_user_not_in_operators_table(tmp_path: Path) -> None:
    operator_files_db = tmp_path / "op_files.db"
    OperatorFileRepository(db_path=str(operator_files_db))
    operators_db = str(tmp_path / "operators.db")
    OperatorRepository(operators_db)
    result = resolve_chat_id_for_username(
        username="@stranger",
        operator_files_db_path=str(operator_files_db),
        operators_db_path=operators_db,
    )
    assert result is None


def test_handles_missing_operator_files_db_but_operators_table_has_chat_id(
    tmp_path: Path,
) -> None:
    missing_path = str(tmp_path / "nope.db")
    operators_db = str(tmp_path / "operators.db")
    op_repo = OperatorRepository(operators_db)
    op_repo.create(username="@alice", project_id=1, chat_id=1234)
    result = resolve_chat_id_for_username(
        username="@alice",
        operator_files_db_path=missing_path,
        operators_db_path=operators_db,
    )
    assert result == 1234


def test_handles_missing_operator_files_db_gracefully(tmp_path: Path) -> None:
    missing_path = tmp_path / "nope.db"
    result = resolve_chat_id_for_username(
        username="@alice",
        operator_files_db_path=str(missing_path),
        operators_db_path=str(tmp_path / "operators.db"),
    )
    assert result is None
