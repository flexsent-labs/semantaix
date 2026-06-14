"""Contract tests for /projects/{slug}/prompts endpoints.

Covers list / get / put / restore / list-versions plus the
admin-or-operator-of-this-project authorization rule.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.admin_auth import AdminAuthRepository
from services.api.app.operators import OperatorRepository
from services.api.app.project_prompts import (
    MAX_PROMPT_VALUE_BYTES,
    ProjectPromptRepository,
    default_prompt,
)
from services.api.app.projects import ProjectRepository
from services.api.app.web_auth import WebAuthRepository


@pytest.fixture
def env(tmp_path, monkeypatch):
    projects = ProjectRepository(str(tmp_path / "projects.sqlite3"))
    operators = OperatorRepository(str(tmp_path / "operators.sqlite3"))
    prompts = ProjectPromptRepository(str(tmp_path / "prompts.sqlite3"))
    web_auth = WebAuthRepository(db_path=str(tmp_path / "web_auth.sqlite3"))
    admin_auth = AdminAuthRepository(str(tmp_path / "admin_auth.sqlite3"))

    monkeypatch.setattr(api_main, "project_repository", projects)
    monkeypatch.setattr(api_main, "operator_repository", operators)
    monkeypatch.setattr(api_main, "project_prompt_repository", prompts)
    monkeypatch.setattr(api_main, "web_auth_repository", web_auth)
    monkeypatch.setattr(api_main, "admin_auth_repository", admin_auth)
    monkeypatch.setattr(
        api_main.admin_auth_service, "web_auth_repository", web_auth
    )
    monkeypatch.setattr(
        api_main.settings, "hitl_config_admin_username", "@admin"
    )
    monkeypatch.setattr(
        api_main.settings, "operators_db_path", str(tmp_path / "operators.sqlite3")
    )
    monkeypatch.setattr(
        api_main.settings, "internal_service_token", "test-bot-token"
    )

    default_project = projects.create(slug="default", name="Default")
    other_project = projects.create(slug="other", name="Other")
    operators.create(username="@alice", project_id=default_project.id, chat_id=11)
    operators.create(username="@bob", project_id=other_project.id, chat_id=22)

    client = TestClient(api_main.app)
    return {
        "client": client,
        "projects": projects,
        "operators": operators,
        "prompts": prompts,
        "web_auth": web_auth,
        "default_project": default_project,
        "other_project": other_project,
    }


def _login_cookie(env, username: str) -> dict[str, str]:
    chat_id = 99 if username == "@admin" else 11
    code = env["web_auth"].create_code(username=username, chat_id=chat_id)
    response = env["client"].post(
        "/admin/auth/verify", json={"username": username, "code": code}
    )
    assert response.status_code == 200, response.text
    return {api_main.settings.web_session_cookie_name: response.cookies.get(
        api_main.settings.web_session_cookie_name
    )}


def _internal_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-bot-token"}


# ---------------------------------------------------------------------------
# Auth + access control
# ---------------------------------------------------------------------------


def test_list_requires_session(env):
    response = env["client"].get("/projects/default/prompts")
    assert response.status_code == 401


def test_list_returns_404_for_unknown_project(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].get("/projects/ghost/prompts", cookies=cookies)
    assert response.status_code == 404
    assert response.json()["detail"] == "project_not_found"


def test_operator_outside_project_is_denied(env):
    cookies = _login_cookie(env, "@alice")
    response = env["client"].get("/projects/other/prompts", cookies=cookies)
    assert response.status_code == 403
    assert response.json()["detail"] == "not_in_project"


def test_operator_in_project_is_allowed(env):
    cookies = _login_cookie(env, "@alice")
    response = env["client"].get("/projects/default/prompts", cookies=cookies)
    assert response.status_code == 200


def test_internal_requires_as_user(env):
    response = env["client"].get(
        "/projects/default/prompts", headers=_internal_headers()
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "missing_as_user"


def test_internal_with_as_user_admin_works(env):
    response = env["client"].get(
        "/projects/default/prompts?as_user=@admin",
        headers=_internal_headers(),
    )
    assert response.status_code == 200


def test_x_admin_session_header_accepted(env):
    """The web UI admin pages authenticate via the X-Admin-Session header."""
    token = api_main.admin_auth_repository.request_code(
        admin_username="@admin", ttl_seconds=300
    )
    session = api_main.admin_auth_repository.consume_code(
        admin_username="@admin", code=token, ttl_seconds=86400
    )
    response = env["client"].get(
        "/projects/default/prompts",
        headers={"X-Admin-Session": session.token},
    )
    assert response.status_code == 200


def test_x_admin_session_invalid_returns_401(env):
    response = env["client"].get(
        "/projects/default/prompts",
        headers={"X-Admin-Session": "not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_admin_session"


def test_internal_with_as_user_unknown_role_is_denied(env):
    response = env["client"].get(
        "/projects/default/prompts?as_user=@nobody",
        headers=_internal_headers(),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# List endpoint
# ---------------------------------------------------------------------------


def test_list_returns_all_items_with_defaults_when_unset(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].get("/projects/default/prompts", cookies=cookies)
    body = response.json()
    assert body["project_slug"] == "default"
    assert len(body["items"]) == 7
    for item in body["items"]:
        assert item["is_default"] is True
        assert item["version"] == 0
        assert item["value"] == default_prompt(item["prompt_name"])


def test_list_shows_overrides_over_defaults(env):
    env["prompts"].set(
        project_id=env["default_project"].id,
        prompt_name="verifier_system",
        value="custom verifier",
        edited_by="@admin",
    )
    cookies = _login_cookie(env, "@admin")
    body = env["client"].get(
        "/projects/default/prompts", cookies=cookies
    ).json()
    by_name = {item["prompt_name"]: item for item in body["items"]}
    assert by_name["verifier_system"]["value"] == "custom verifier"
    assert by_name["verifier_system"]["is_default"] is False
    assert by_name["verifier_system"]["version"] == 1


# ---------------------------------------------------------------------------
# Get endpoint
# ---------------------------------------------------------------------------


def test_get_unknown_name_returns_404(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].get(
        "/projects/default/prompts/bogus", cookies=cookies
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "unknown_prompt_name"


def test_get_returns_default_with_empty_history(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].get(
        "/projects/default/prompts/verifier_system", cookies=cookies
    )
    assert response.status_code == 200
    body = response.json()
    assert body["is_default"] is True
    assert body["history"] == []
    assert body["value"] == default_prompt("verifier_system")


def test_get_returns_override_with_history(env):
    project_id = env["default_project"].id
    env["prompts"].set(
        project_id=project_id,
        prompt_name="verifier_system",
        value="v1",
        edited_by="@a",
    )
    env["prompts"].set(
        project_id=project_id,
        prompt_name="verifier_system",
        value="v2",
        edited_by="@b",
    )
    cookies = _login_cookie(env, "@admin")
    body = env["client"].get(
        "/projects/default/prompts/verifier_system", cookies=cookies
    ).json()
    assert body["value"] == "v2"
    assert body["version"] == 2
    assert [pv["version"] for pv in body["history"]] == [2, 1]


# ---------------------------------------------------------------------------
# Put endpoint
# ---------------------------------------------------------------------------


def test_put_unknown_name_returns_404(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].put(
        "/projects/default/prompts/bogus",
        cookies=cookies,
        json={"value": "x"},
    )
    assert response.status_code == 404


def test_put_writes_value_and_returns_version(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].put(
        "/projects/default/prompts/verifier_system",
        cookies=cookies,
        json={"value": "fresh verifier"},
    )
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert env["prompts"].get(
        project_id=env["default_project"].id, prompt_name="verifier_system"
    ) == "fresh verifier"


def test_put_invalid_grounding_returns_422(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].put(
        "/projects/default/prompts/grounding_system",
        cookies=cookies,
        json={"value": "missing placeholders"},
    )
    assert response.status_code == 422


def test_put_oversize_returns_413(env):
    cookies = _login_cookie(env, "@admin")
    big = "x" * (MAX_PROMPT_VALUE_BYTES + 1)
    response = env["client"].put(
        "/projects/default/prompts/verifier_system",
        cookies=cookies,
        json={"value": big},
    )
    assert response.status_code == 413


def test_put_records_edited_by_principal(env):
    cookies = _login_cookie(env, "@alice")
    env["client"].put(
        "/projects/default/prompts/verifier_system",
        cookies=cookies,
        json={"value": "alice's text"},
    )
    current = env["prompts"].get_current(
        project_id=env["default_project"].id, prompt_name="verifier_system"
    )
    assert current is not None
    assert current.updated_by == "@alice"


# ---------------------------------------------------------------------------
# Restore endpoint
# ---------------------------------------------------------------------------


def test_restore_unknown_name_returns_404(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].post(
        "/projects/default/prompts/bogus/restore",
        cookies=cookies,
        json={"version": 1},
    )
    assert response.status_code == 404


def test_restore_unknown_version_returns_404(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].post(
        "/projects/default/prompts/verifier_system/restore",
        cookies=cookies,
        json={"version": 99},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "version_not_found"


def test_restore_brings_back_old_value_as_new_version(env):
    project_id = env["default_project"].id
    env["prompts"].set(
        project_id=project_id,
        prompt_name="verifier_system",
        value="v1",
        edited_by="@a",
    )
    env["prompts"].set(
        project_id=project_id,
        prompt_name="verifier_system",
        value="v2",
        edited_by="@b",
    )
    cookies = _login_cookie(env, "@admin")
    response = env["client"].post(
        "/projects/default/prompts/verifier_system/restore",
        cookies=cookies,
        json={"version": 1},
    )
    assert response.json()["version"] == 3
    assert env["prompts"].get(
        project_id=project_id, prompt_name="verifier_system"
    ) == "v1"


# ---------------------------------------------------------------------------
# List versions endpoint
# ---------------------------------------------------------------------------


def test_list_versions_unknown_name_returns_404(env):
    cookies = _login_cookie(env, "@admin")
    response = env["client"].get(
        "/projects/default/prompts/bogus/versions", cookies=cookies
    )
    assert response.status_code == 404


def test_list_versions_returns_history(env):
    project_id = env["default_project"].id
    for v in ("a", "b", "c"):
        env["prompts"].set(
            project_id=project_id,
            prompt_name="verifier_system",
            value=v,
            edited_by="@x",
        )
    cookies = _login_cookie(env, "@admin")
    body = env["client"].get(
        "/projects/default/prompts/verifier_system/versions",
        cookies=cookies,
    ).json()
    assert [item["version"] for item in body["items"]] == [3, 2, 1]


def test_list_versions_respects_limit(env):
    project_id = env["default_project"].id
    for v in ("a", "b", "c"):
        env["prompts"].set(
            project_id=project_id,
            prompt_name="verifier_system",
            value=v,
            edited_by="@x",
        )
    cookies = _login_cookie(env, "@admin")
    body = env["client"].get(
        "/projects/default/prompts/verifier_system/versions?limit=2",
        cookies=cookies,
    ).json()
    assert [item["version"] for item in body["items"]] == [3, 2]


# ---------------------------------------------------------------------------
# Pending-edit endpoints (multi-step bot flow)
# ---------------------------------------------------------------------------


def test_arm_pending_creates_state(env):
    cookies = _login_cookie(env, "@alice")
    response = env["client"].post(
        "/projects/default/prompts/verifier_system/pending",
        cookies=cookies,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["armed_for"] == "@alice"
    assert body["prompt_name"] == "verifier_system"


def test_arm_pending_unknown_name_returns_404(env):
    cookies = _login_cookie(env, "@alice")
    response = env["client"].post(
        "/projects/default/prompts/bogus/pending",
        cookies=cookies,
    )
    assert response.status_code == 404


def test_peek_pending_returns_404_when_none(env):
    cookies = _login_cookie(env, "@alice")
    response = env["client"].get("/pending-prompt-edits", cookies=cookies)
    assert response.status_code == 404


def test_peek_pending_returns_payload_when_armed(env):
    cookies = _login_cookie(env, "@alice")
    env["client"].post(
        "/projects/default/prompts/verifier_system/pending", cookies=cookies
    )
    response = env["client"].get("/pending-prompt-edits", cookies=cookies)
    assert response.status_code == 200
    body = response.json()
    assert body["project_slug"] == "default"
    assert body["prompt_name"] == "verifier_system"


def test_cancel_pending_returns_deleted_flag(env):
    cookies = _login_cookie(env, "@alice")
    env["client"].post(
        "/projects/default/prompts/verifier_system/pending", cookies=cookies
    )
    response = env["client"].request(
        "DELETE", "/pending-prompt-edits", cookies=cookies
    )
    assert response.json()["deleted"] is True
    # Second delete is a no-op.
    assert (
        env["client"]
        .request("DELETE", "/pending-prompt-edits", cookies=cookies)
        .json()["deleted"]
        is False
    )


def test_consume_pending_applies_value_and_returns_version(env):
    cookies = _login_cookie(env, "@alice")
    env["client"].post(
        "/projects/default/prompts/verifier_system/pending", cookies=cookies
    )
    response = env["client"].post(
        "/pending-prompt-edits/consume",
        cookies=cookies,
        json={"value": "fresh verifier"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["prompt_name"] == "verifier_system"
    assert env["prompts"].get(
        project_id=env["default_project"].id, prompt_name="verifier_system"
    ) == "fresh verifier"


def test_consume_pending_404_when_none_armed(env):
    cookies = _login_cookie(env, "@alice")
    response = env["client"].post(
        "/pending-prompt-edits/consume",
        cookies=cookies,
        json={"value": "x"},
    )
    assert response.status_code == 404


def test_consume_pending_oversize_returns_413(env):
    cookies = _login_cookie(env, "@alice")
    env["client"].post(
        "/projects/default/prompts/verifier_system/pending", cookies=cookies
    )
    big = "x" * (MAX_PROMPT_VALUE_BYTES + 1)
    response = env["client"].post(
        "/pending-prompt-edits/consume",
        cookies=cookies,
        json={"value": big},
    )
    assert response.status_code == 413


def test_consume_pending_invalid_grounding_returns_422(env):
    cookies = _login_cookie(env, "@alice")
    env["client"].post(
        "/projects/default/prompts/grounding_system/pending", cookies=cookies
    )
    response = env["client"].post(
        "/pending-prompt-edits/consume",
        cookies=cookies,
        json={"value": "missing placeholders"},
    )
    assert response.status_code == 422
