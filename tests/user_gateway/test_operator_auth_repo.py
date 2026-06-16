from __future__ import annotations

import pytest

from services.user_gateway.app.operator_auth_repo import OperatorTelegramAuthRepository


def test_operator_auth_repo_defaults_to_idle(tmp_path) -> None:
    repo = OperatorTelegramAuthRepository(str(tmp_path / "operators.db"))
    assert repo.get(1) is None
    assert repo.get_phase(1) == "idle"


def test_operator_auth_repo_upsert_and_read(tmp_path) -> None:
    repo = OperatorTelegramAuthRepository(str(tmp_path / "operators.db"))
    repo.upsert(
        operator_id=7,
        phase="qr_pending",
        session_path=str(tmp_path / "sessions" / "7.session"),
        linked_username="operator_7",
        customer_channel_active=True,
    )
    record = repo.get(7)
    assert record is not None
    assert record.operator_id == 7
    assert record.phase == "qr_pending"
    assert record.linked_username == "operator_7"
    assert record.customer_channel_active is True


def test_operator_auth_repo_updates_phase_username_and_channel(tmp_path) -> None:
    repo = OperatorTelegramAuthRepository(str(tmp_path / "operators.db"))
    repo.upsert(
        operator_id=8,
        phase="qr_pending",
        session_path=str(tmp_path / "sessions" / "8.session"),
    )
    repo.set_phase(8, "authenticated")
    repo.set_linked_username(8, "operator_8")
    repo.set_customer_channel_active(8, True)
    updated = repo.get(8)
    assert updated is not None
    assert updated.phase == "authenticated"
    assert updated.linked_username == "operator_8"
    assert updated.customer_channel_active is True


def test_operator_auth_repo_missing_operator_raises(tmp_path) -> None:
    repo = OperatorTelegramAuthRepository(str(tmp_path / "operators.db"))
    with pytest.raises(KeyError):
        repo.set_phase(999, "authenticated")
    with pytest.raises(KeyError):
        repo.set_linked_username(999, "ghost")
    with pytest.raises(KeyError):
        repo.set_customer_channel_active(999, True)


def test_operator_auth_repo_clears_stale_rows_on_startup(tmp_path) -> None:
    repo = OperatorTelegramAuthRepository(str(tmp_path / "operators.db"))
    repo.upsert(operator_id=1, phase="qr_pending", session_path=str(tmp_path / "1.session"))
    repo.upsert(operator_id=2, phase="2fa_pending", session_path=str(tmp_path / "2.session"))
    repo.upsert(operator_id=3, phase="authenticated", session_path=str(tmp_path / "3.session"))
    cleared = repo.clear_stale_on_startup()
    assert sorted(cleared) == [1, 2]
    assert repo.get_phase(1) == "idle"
    assert repo.get_phase(2) == "idle"
    assert repo.get_phase(3) == "authenticated"
