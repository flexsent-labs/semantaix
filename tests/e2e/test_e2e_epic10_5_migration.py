"""Epic 10.5 story 10.5-01 E2E: migration smoke — no primary-operator keys in env or DB."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.epic("10.5")]


@pytest.mark.story("10.5-01")
def test_settings_has_no_primary_operator_fields():
    """AppSettings must not expose hitl_primary_operator_{username,chat_id}."""
    from platform_common.settings import AppSettings

    s = AppSettings()
    assert not hasattr(s, "hitl_primary_operator_username"), (
        "hitl_primary_operator_username must have been removed from Settings"
    )
    assert not hasattr(s, "hitl_primary_operator_chat_id"), (
        "hitl_primary_operator_chat_id must have been removed from Settings"
    )


@pytest.mark.story("10.5-01")
def test_bootstrap_seeds_operator_row(tmp_path):
    """After bootstrap, operators table contains the configured alert username."""
    from unittest.mock import patch

    import services.api.app.main as mod
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    proj = ProjectRepository(str(tmp_path / "proj.db"))
    default = proj.ensure_default_project()

    with (
        patch.object(mod, "hitl_ticket_repository", hitl),
        patch.object(mod, "operator_repository", ops),
        patch.object(mod, "project_repository", proj),
    ):
        mod._bootstrap_default_entities()

    seeded = ops.find_by_username(mod.settings.telegram_alert_username)
    assert seeded is not None
    assert seeded.project_id == default.id
    assert seeded.is_active


@pytest.mark.story("10.5-01")
def test_bootstrap_removes_primary_operator_runtime_config_rows(tmp_path):
    """Bootstrap deletes stale hitl_primary_operator_* keys from hitl_runtime_config."""
    from unittest.mock import patch

    import services.api.app.main as mod
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    proj = ProjectRepository(str(tmp_path / "proj.db"))

    hitl.set_runtime_config(
        key="hitl_primary_operator_username", value="@legacy", updated_by="seed"
    )
    hitl.set_runtime_config(
        key="hitl_primary_operator_chat_id", value="9999", updated_by="seed"
    )

    with (
        patch.object(mod, "hitl_ticket_repository", hitl),
        patch.object(mod, "operator_repository", ops),
        patch.object(mod, "project_repository", proj),
    ):
        mod._bootstrap_default_entities()

    assert hitl.get_runtime_config("hitl_primary_operator_username") is None
    assert hitl.get_runtime_config("hitl_primary_operator_chat_id") is None


@pytest.mark.story("10.5-01")
def test_bootstrap_idempotent_second_run(tmp_path):
    """A second bootstrap call must not raise or duplicate the operator row."""
    from unittest.mock import patch

    import services.api.app.main as mod
    from services.api.app.hitl import HitlTicketRepository
    from services.api.app.operators import OperatorRepository
    from services.api.app.projects import ProjectRepository

    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    ops = OperatorRepository(str(tmp_path / "ops.db"))
    proj = ProjectRepository(str(tmp_path / "proj.db"))

    with (
        patch.object(mod, "hitl_ticket_repository", hitl),
        patch.object(mod, "operator_repository", ops),
        patch.object(mod, "project_repository", proj),
    ):
        mod._bootstrap_default_entities()
        mod._bootstrap_default_entities()

    active = ops.list_active()
    usernames = [o.username for o in active]
    assert usernames.count(mod.settings.telegram_alert_username) == 1
