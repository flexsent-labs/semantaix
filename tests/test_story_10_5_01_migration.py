"""Story 10.5-01 — settings + hitl_runtime_config cleanup migration."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _bootstrap(tmp_path: Path) -> tuple[str, str, str]:
    """Bootstrap with isolated DBs, return (hitl_db, operators_db, projects_db)."""
    hitl_db = str(tmp_path / "hitl.db")
    operators_db = str(tmp_path / "operators.db")
    projects_db = str(tmp_path / "projects.db")
    return hitl_db, operators_db, projects_db


def _run_bootstrap(hitl_db: str, operators_db: str, projects_db: str) -> None:
    import services.api.app.main as mod

    with (
        patch.object(mod, "hitl_ticket_repository") as mock_hitl,
        patch.object(mod, "operator_repository") as mock_op,
        patch.object(mod, "project_repository") as mock_proj,
    ):
        from services.api.app.hitl import HitlTicketRepository
        from services.api.app.operators import OperatorRepository
        from services.api.app.projects import ProjectRepository

        real_hitl = HitlTicketRepository(hitl_db)
        real_op = OperatorRepository(operators_db)
        real_proj = ProjectRepository(projects_db)
        mock_hitl.delete_runtime_config.side_effect = real_hitl.delete_runtime_config
        mock_hitl.set_runtime_config.side_effect = real_hitl.set_runtime_config
        mock_hitl.get_runtime_config.side_effect = real_hitl.get_runtime_config
        mock_op.ensure_default_operator.side_effect = real_op.ensure_default_operator
        mock_proj.ensure_default_project.return_value = real_proj.ensure_default_project()

        mod._bootstrap_default_entities()

    return real_hitl, real_op


def test_migration_deletes_primary_operator_runtime_config_rows(tmp_path):
    hitl_db, operators_db, projects_db = _bootstrap(tmp_path)

    from services.api.app.hitl import HitlTicketRepository

    hitl = HitlTicketRepository(hitl_db)
    hitl.set_runtime_config(
        key="hitl_primary_operator_username", value="@old_op", updated_by="test"
    )
    hitl.set_runtime_config(
        key="hitl_primary_operator_chat_id", value="12345", updated_by="test"
    )

    real_hitl, _ = _run_bootstrap(hitl_db, operators_db, projects_db)

    assert real_hitl.get_runtime_config("hitl_primary_operator_username") is None
    assert real_hitl.get_runtime_config("hitl_primary_operator_chat_id") is None


def test_migration_idempotent_when_rows_absent(tmp_path):
    hitl_db, operators_db, projects_db = _bootstrap(tmp_path)

    # Run twice — second run must not raise
    _run_bootstrap(hitl_db, operators_db, projects_db)
    _run_bootstrap(hitl_db, operators_db, projects_db)

    from services.api.app.hitl import HitlTicketRepository

    hitl = HitlTicketRepository(hitl_db)
    assert hitl.get_runtime_config("hitl_primary_operator_username") is None
    assert hitl.get_runtime_config("hitl_primary_operator_chat_id") is None


def test_migration_leaves_default_operator_seeded(tmp_path):
    hitl_db, operators_db, projects_db = _bootstrap(tmp_path)

    _, real_op = _run_bootstrap(hitl_db, operators_db, projects_db)

    from platform_common.settings import get_settings

    settings = get_settings()
    operator = real_op.find_by_username(settings.hitl_primary_operator_username)
    assert operator is not None
    assert operator.is_active


def test_delete_runtime_config_no_op_when_key_absent(tmp_path):
    from services.api.app.hitl import HitlTicketRepository

    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    # Should not raise
    hitl.delete_runtime_config("nonexistent_key")
    assert hitl.get_runtime_config("nonexistent_key") is None


def test_delete_runtime_config_removes_existing_key(tmp_path):
    from services.api.app.hitl import HitlTicketRepository

    hitl = HitlTicketRepository(str(tmp_path / "hitl.db"))
    hitl.set_runtime_config(key="foo", value="bar", updated_by="test")
    assert hitl.get_runtime_config("foo") == "bar"

    hitl.delete_runtime_config("foo")
    assert hitl.get_runtime_config("foo") is None
