import logging
import sqlite3

import pytest
from cryptography.fernet import Fernet

from services.api.app.calendar.token_repository import (
    STATUS_CONNECTED,
    STATUS_RECONNECT_NEEDED,
    CalendarTokenRepository,
    TokenNotFound,
    init_token_schema,
)


def _repo(tmp_path) -> CalendarTokenRepository:
    return CalendarTokenRepository(
        db_path=str(tmp_path / "calendar.sqlite3"),
        fernet=Fernet(Fernet.generate_key()),
    )


def test_init_schema_creates_table(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    _repo_path = CalendarTokenRepository(
        db_path=path, fernet=Fernet(Fernet.generate_key())
    )
    _repo_path.init_schema()
    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "calendar_operator_tokens" in names
    assert "calendar_connect_notifications" in names


def test_init_token_schema_without_key(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    init_token_schema(path)
    init_token_schema(path)  # idempotent
    with sqlite3.connect(path) as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "calendar_operator_tokens" in names


def test_encrypt_decrypt_round_trip(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert(1, "@op", "secret-refresh-token")
    assert repo.get_refresh_token(1, "@op") == "secret-refresh-token"


def test_get_refresh_token_raises_when_missing(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(TokenNotFound):
        repo.get_refresh_token(1, "@nobody")


def test_upsert_overwrites(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert(1, "@op", "first-token")
    repo.upsert(1, "@op", "second-token")
    assert repo.get_refresh_token(1, "@op") == "second-token"


def test_set_status_and_delete(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    repo = CalendarTokenRepository(db_path=path, fernet=Fernet(Fernet.generate_key()))
    repo.upsert(1, "@op", "token")
    repo.set_status(1, "@op", STATUS_RECONNECT_NEEDED)
    with sqlite3.connect(path) as connection:
        status = connection.execute(
            "SELECT status FROM calendar_operator_tokens "
            "WHERE project_id = ? AND operator = ?",
            (1, "@op"),
        ).fetchone()[0]
    assert status == STATUS_RECONNECT_NEEDED
    assert STATUS_CONNECTED == "connected"

    repo.delete(1, "@op")
    with pytest.raises(TokenNotFound):
        repo.get_refresh_token(1, "@op")


def test_connection_notification_claim_is_persistent_and_reset_on_delete(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    key = Fernet.generate_key()
    repo = CalendarTokenRepository(db_path=path, fernet=Fernet(key))

    assert repo.claim_connection_notification(1, "@op") is True
    assert repo.claim_connection_notification(1, "@op") is False

    restarted_repo = CalendarTokenRepository(db_path=path, fernet=Fernet(key))
    assert restarted_repo.claim_connection_notification(1, "@op") is False

    restarted_repo.delete(1, "@op")
    assert restarted_repo.claim_connection_notification(1, "@op") is True


def test_backfill_connection_notification_claims_marks_connected_rows(tmp_path):
    repo = _repo(tmp_path)
    repo.upsert(1, "@connected", "token-1")
    repo.upsert(2, "@reconnect", "token-2")
    repo.set_status(2, "@reconnect", STATUS_RECONNECT_NEEDED)

    assert repo.backfill_connection_notification_claims() == 1
    assert repo.backfill_connection_notification_claims() == 0
    assert repo.claim_connection_notification(1, "@connected") is False
    assert repo.claim_connection_notification(2, "@reconnect") is True


def test_get_status_returns_none_for_missing_row(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    repo = CalendarTokenRepository(db_path=path, fernet=Fernet(Fernet.generate_key()))
    assert repo.get_status(1, "@nobody") is None


def test_get_status_returns_current_status_and_reflects_set_status(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    repo = CalendarTokenRepository(db_path=path, fernet=Fernet(Fernet.generate_key()))
    repo.upsert(1, "@op", "token")
    assert repo.get_status(1, "@op") == STATUS_CONNECTED
    repo.set_status(1, "@op", STATUS_RECONNECT_NEEDED)
    assert repo.get_status(1, "@op") == STATUS_RECONNECT_NEEDED
    # Upsert overwrites status back to 'connected' (the recovery path: operator
    # successfully re-runs /connect_calendar → status flips back → next
    # dead-token cycle can re-DM, since the dedup is unlatched).
    repo.upsert(1, "@op", "new-token")
    assert repo.get_status(1, "@op") == STATUS_CONNECTED


def test_stored_blob_is_not_plaintext(tmp_path):
    path = str(tmp_path / "calendar.sqlite3")
    repo = CalendarTokenRepository(db_path=path, fernet=Fernet(Fernet.generate_key()))
    plaintext = "super-secret-refresh-token"
    repo.upsert(1, "@op", plaintext)
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT refresh_token_encrypted FROM calendar_operator_tokens "
            "WHERE project_id = ? AND operator = ?",
            (1, "@op"),
        ).fetchone()[0]
    assert plaintext.encode("utf-8") not in bytes(stored)
    assert stored != plaintext


def test_token_and_key_never_logged(tmp_path, caplog):
    key = Fernet.generate_key()
    repo = CalendarTokenRepository(db_path=str(tmp_path / "c.sqlite3"), fernet=Fernet(key))
    plaintext = "leaky-refresh-token"
    with caplog.at_level(logging.DEBUG):
        repo.upsert(1, "@op", plaintext)
        repo.get_refresh_token(1, "@op")
        repo.set_status(1, "@op", STATUS_RECONNECT_NEEDED)
        repo.delete(1, "@op")
    logged = caplog.text
    assert plaintext not in logged
    assert key.decode("utf-8") not in logged
