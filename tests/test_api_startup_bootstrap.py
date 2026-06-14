from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.operators import OperatorRepository
from services.api.app.projects import ProjectRepository


def _swap_singletons(monkeypatch, tmp_path):
    fresh_projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    fresh_operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    monkeypatch.setattr(api_main, "project_repository", fresh_projects)
    monkeypatch.setattr(api_main, "operator_repository", fresh_operators)
    return fresh_projects, fresh_operators


def test_bootstrap_creates_default_project_and_primary_operator(tmp_path, monkeypatch):
    """Calling `_bootstrap_default_entities` on a fresh stack lands the rows."""
    projects, operators = _swap_singletons(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api_main.settings, "telegram_alert_username", "@bootstrap_op"
    )
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", "42")
    api_main._bootstrap_default_entities()

    default = projects.get_by_slug("default")
    assert default is not None
    assert default.id == 1
    primary = operators.find_by_username("@bootstrap_op")
    assert primary is not None
    assert primary.project_id == default.id
    assert primary.chat_id == 42

    # Idempotent on re-run.
    api_main._bootstrap_default_entities()
    assert len(projects.list_all()) == 1
    assert len(operators.list_all()) == 1


def test_bootstrap_handles_missing_primary_chat_id(tmp_path, monkeypatch):
    _, operators = _swap_singletons(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api_main.settings, "telegram_alert_username", "@no_chat"
    )
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    api_main._bootstrap_default_entities()
    operator = operators.find_by_username("@no_chat")
    assert operator is not None
    assert operator.chat_id is None


def test_bootstrap_ignores_non_numeric_chat_id(tmp_path, monkeypatch):
    _, operators = _swap_singletons(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api_main.settings, "telegram_alert_username", "@junk_chat"
    )
    monkeypatch.setattr(
        api_main.settings, "telegram_alert_chat_id", "not-a-number"
    )
    api_main._bootstrap_default_entities()
    operator = operators.find_by_username("@junk_chat")
    assert operator is not None
    assert operator.chat_id is None


def test_api_app_has_bootstrapped_default_project_on_import(tmp_path, monkeypatch):
    """Default project + alert operator row exist after a fresh bootstrap.

    Bootstrap now seeds from telegram_alert_username so the operators table
    is always populated on a fresh deployment without any settings duplication.
    The swap below isolates the bootstrap against a fresh DB to keep the
    assertion stable across test order.
    """
    projects, operators = _swap_singletons(monkeypatch, tmp_path)
    monkeypatch.setattr(
        api_main.settings, "telegram_alert_username", "@import_op"
    )
    monkeypatch.setattr(api_main.settings, "telegram_alert_chat_id", None)
    api_main._bootstrap_default_entities()
    default = projects.get_by_slug("default")
    assert default is not None
    primary = operators.find_by_username("@import_op")
    assert primary is not None
    assert primary.project_id == default.id

    client = TestClient(api_main.app)
    response = client.get("/health/live")
    assert response.status_code == 200
