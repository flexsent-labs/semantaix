from __future__ import annotations

from services.user_gateway.app.auth_session_repo import AuthSessionRepository


def test_auth_session_repo_defaults_to_idle(tmp_path) -> None:
    repo = AuthSessionRepository(str(tmp_path / "auth.db"))
    assert repo.get_phase() == "idle"


def test_auth_session_repo_set_and_get_phase(tmp_path) -> None:
    repo = AuthSessionRepository(str(tmp_path / "auth.db"))
    repo.set_phase("qr_pending")
    assert repo.get_phase() == "qr_pending"


def test_auth_session_repo_clears_stale_phases(tmp_path) -> None:
    repo = AuthSessionRepository(str(tmp_path / "auth.db"))
    repo.set_phase("2fa_pending")
    stale = repo.clear_stale_on_startup()
    assert stale == "2fa_pending"
    assert repo.get_phase() == "idle"


def test_auth_session_repo_keeps_authenticated(tmp_path) -> None:
    repo = AuthSessionRepository(str(tmp_path / "auth.db"))
    repo.set_phase("authenticated")
    assert repo.clear_stale_on_startup() == "authenticated"
