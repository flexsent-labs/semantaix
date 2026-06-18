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


def test_bootstrap_creates_default_project_only(tmp_path, monkeypatch):
    """Bootstrap seeds the default project but not an operator row."""
    projects, operators = _swap_singletons(monkeypatch, tmp_path)
    api_main._bootstrap_default_entities()

    default = projects.get_by_slug("default")
    assert default is not None
    assert default.id == 1
    assert operators.list_all() == []

    api_main._bootstrap_default_entities()
    assert len(projects.list_all()) == 1
    assert len(operators.list_all()) == 0


def test_bootstrap_deactivates_admin_operator_rows(tmp_path, monkeypatch):
    projects, operators = _swap_singletons(monkeypatch, tmp_path)
    default = projects.ensure_default_project()
    operators.create(username="@ajdevy", project_id=default.id, chat_id=42)
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@ajdevy")
    monkeypatch.setattr(api_main.settings, "hitl_config_admin_username", "@ajdevy")

    api_main._bootstrap_default_entities()

    admin = operators.find_by_username("@ajdevy")
    assert admin is not None
    assert admin.is_active is False


def test_create_operator_rejects_platform_admin_username(tmp_path, monkeypatch):
    projects, _operators = _swap_singletons(monkeypatch, tmp_path)
    default = projects.ensure_default_project()
    monkeypatch.setattr(api_main.settings, "admin_telegram_username", "@ajdevy")
    monkeypatch.setattr(api_main.settings, "admin_internal_token", "secret")
    client = TestClient(api_main.app)
    response = client.post(
        "/operators",
        json={"username": "@ajdevy", "project_id": default.id, "chat_id": 1},
        headers={"X-Internal-Token": "secret"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "platform_admin_not_operator"


def test_api_app_has_bootstrapped_default_project_on_import(tmp_path, monkeypatch):
    projects, operators = _swap_singletons(monkeypatch, tmp_path)
    api_main._bootstrap_default_entities()
    default = projects.get_by_slug("default")
    assert default is not None
    assert operators.list_all() == []

    client = TestClient(api_main.app)
    response = client.get("/health/live")
    assert response.status_code == 200
