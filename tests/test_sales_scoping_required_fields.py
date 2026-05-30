"""Story 12.12 — configurable required scoping fields."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.main import (
    _effective_scoping_required_fields,
    _effective_scoping_schema,
    hitl_ticket_repository,
    sales_services_repository,
    scoping_schema_repository,
    settings,
)
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    SCOPING_COMPLETE_HANDOFF_LINE,
    SalesPersonaAnswerer,
)
from services.api.app.sales.scoping_schema import (
    TRANSFER_SCHEMA,
    ScopingField,
    ScopingSchema,
)
from services.api.app.sales.scoping_schema_repository import (
    PROJECT_DEFAULT_SERVICE_ID,
)
from services.api.app.sales.scoping_schema_repository import (
    init_schema as init_scoping_schema,
)

_NOW = datetime(2026, 5, 30, 9, 0, tzinfo=UTC)
_RENTAL = ("dates", "headcount", "vehicle_count")  # drops difficulty + drivers


# --- Intent completeness against a required subset --------------------------


def test_is_complete_against_required_subset() -> None:
    intent = Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1)
    assert intent.is_complete(_RENTAL) is True          # subset satisfied
    assert intent.is_complete() is False                # all five → still missing 2
    assert intent.missing_fields(_RENTAL) == []
    assert intent.missing_fields() == ["difficulty", "drivers"]


# --- _effective_scoping_required_fields resolver ----------------------------


def _wire_hitl(tmp_path) -> None:
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")


def test_resolver_uses_settings_default(tmp_path, monkeypatch) -> None:
    _wire_hitl(tmp_path)
    monkeypatch.setattr(settings, "scoping_required_fields", "dates,headcount,vehicle_count")
    assert _effective_scoping_required_fields() == _RENTAL


def test_resolver_runtime_override_and_canonical_order(tmp_path, monkeypatch) -> None:
    _wire_hitl(tmp_path)
    hitl_ticket_repository.set_runtime_config(
        key="scoping_required_fields", value="drivers, dates", updated_by="@t"
    )
    try:
        # Canonical order enforced regardless of config order.
        assert _effective_scoping_required_fields() == ("dates", "drivers")
    finally:
        hitl_ticket_repository.set_runtime_config(
            key="scoping_required_fields", value="", updated_by="@t"
        )


def test_resolver_drops_unknown_names(tmp_path, monkeypatch) -> None:
    _wire_hitl(tmp_path)
    monkeypatch.setattr(settings, "scoping_required_fields", "dates,bogus,headcount")
    assert _effective_scoping_required_fields() == ("dates", "headcount")


def test_resolver_all_invalid_falls_back_to_all_five(tmp_path, monkeypatch) -> None:
    _wire_hitl(tmp_path)
    monkeypatch.setattr(settings, "scoping_required_fields", "nope,bogus")
    assert _effective_scoping_required_fields() == (
        "dates", "headcount", "vehicle_count", "difficulty", "drivers"
    )


# --- _effective_scoping_schema (Story 12.16) --------------------------------


def _schema_ctx(project_id: int | None) -> AnswerContext:
    return AnswerContext(
        chat_id=7, customer_username="@a", trace_id="t",
        now=_NOW, project_id=project_id,
    )


def test_effective_schema_without_project_uses_narrowed_transfer(
    tmp_path, monkeypatch
) -> None:
    _wire_hitl(tmp_path)
    monkeypatch.setattr(settings, "scoping_required_fields", "dates,headcount")
    schema = _effective_scoping_schema(_schema_ctx(None))
    assert schema.required_keys() == ("dates", "headcount")
    assert schema.keys() == TRANSFER_SCHEMA.keys()


def test_effective_schema_returns_configured_project_default(
    tmp_path, monkeypatch
) -> None:
    scoping_schema_repository.db_path = str(tmp_path / "sch.db")
    init_scoping_schema(scoping_schema_repository.db_path)
    custom = ScopingSchema(
        (ScopingField("dates", "Когда?"), ScopingField("topic", "Тема?"))
    )
    scoping_schema_repository.set_schema(
        project_id=1,
        service_id=PROJECT_DEFAULT_SERVICE_ID,
        schema=custom,
        updated_by="@t",
    )
    monkeypatch.setattr(
        sales_services_repository, "list_for_project", lambda *, project_id: []
    )
    assert _effective_scoping_schema(_schema_ctx(1)) == custom


# --- answerer honours the configured set ------------------------------------


class _FakeStateRepo:
    def __init__(self, *, initial: dict[str, Any]) -> None:
        self._state = initial
        self.upserts: list[dict[str, Any]] = []

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._state

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)


class _NoOpServicesRepo:
    def count_active(self, *, project_id):  # pragma: no cover
        return 0

    def list_for_project(self, *, project_id):  # pragma: no cover
        return []

    def get_by_name(self, *, project_id, name):  # pragma: no cover
        return None


class _QueueOpenRouter:
    def __init__(self, *responses: dict[str, Any]) -> None:
        self._responses = list(responses)

    async def complete_json(self, *, system, user, model=None) -> dict[str, Any]:
        return self._responses.pop(0)


def _build(intent: Intent, getter) -> tuple[SalesPersonaAnswerer, _FakeStateRepo]:
    repo = _FakeStateRepo(
        initial={
            "chat_id": 7, "project_id": 1, "current_stage": "scoping",
            "collected_intent": intent.to_dict(), "last_proposal": None,
        }
    )
    answerer = SalesPersonaAnswerer(
        state_repo=repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=_QueueOpenRouter({"extracted_fields": {}, "next_question": "?"}),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        calendar_settings_repo=None,
        scoping_required_fields_getter=getter,
    )
    return answerer, repo


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7, customer_username="@a", trace_id="t", now=_NOW, project_id=1
    )


@pytest.mark.asyncio
async def test_funnel_completes_after_required_subset_only() -> None:
    # The three rental fields are filled; difficulty/drivers are NOT required,
    # so the funnel completes without ever asking them.
    intent = Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1)
    answerer, _ = _build(intent, lambda: _RENTAL)
    result = await answerer.try_answer(question="всё", ctx=_ctx())
    assert result.text == SCOPING_COMPLETE_HANDOFF_LINE


@pytest.mark.asyncio
async def test_empty_getter_falls_back_to_all_five() -> None:
    # Getter returns () → answerer falls back to all five → still missing
    # difficulty/drivers → stays in scoping (asks the next field).
    intent = Intent(dates="завтра в 14:00", headcount=2, vehicle_count=1)
    answerer, _ = _build(intent, lambda: ())
    result = await answerer.try_answer(question="что дальше?", ctx=_ctx())
    assert result.metadata["stage_after"] == "scoping"
