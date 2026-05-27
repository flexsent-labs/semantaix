"""Epic 12, Story 12.07 — date proposal turn via Epic 11 calendar.

The pipeline wiring for ``SalesPersonaAnswerer`` lands in a later story,
so the integration drives the answerer + a real :class:`DateProposer`
end-to-end with sqlite-backed state and project-services repositories.
The Epic-11 calendar backend is stubbed by passing an in-memory
``availability_compute`` callable into the proposer.

Two scenarios:

  1. Calendar enabled + a free slot for ``1 мая 14:00`` → bot proposes,
     customer accepts ("да"), bot moves to ``closing`` with the handoff
     line and ``hitl_reason='sales_closing_handoff'``.
  2. Calendar disabled → bot delivers the fixed Russian fallback and
     escalates with ``hitl_reason='date_calendar_disabled'``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.calendar.project_services_repository import (
    ProjectServiceRepository,
)
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import (
    AvailabilitySlot,
    DateProposer,
)
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    CLOSING_HANDOFF_LINE,
    HITL_REASON_CALENDAR_DISABLED,
    HITL_REASON_CLOSING_HANDOFF,
    PROPOSAL_FALLBACK_CALENDAR_DISABLED,
    STAGE_CLOSING,
    STAGE_PROPOSING,
    SalesPersonaAnswerer,
)
from services.api.app.sales.state_repository import StateRepository

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.epic("12"),
    pytest.mark.story("12-07"),
]


@dataclass
class _ServiceRow:
    id: int
    name: str
    description: str | None


class _InnerServicesRepo:
    """The DateProposer-side services repo — returns only ``id`` + ``name``."""

    def __init__(self, repo: ProjectServiceRepository) -> None:
        self._repo = repo

    def list_for_project(self, *, project_id: int) -> list[_ServiceRow]:
        return [
            _ServiceRow(id=row.id, name=row.name, description=row.description)
            for row in self._repo.list_for_project(project_id=project_id)
        ]


class _OuterServicesRepo:
    """The answerer-side services repo — supports the catalog / concept asides."""

    def __init__(self, repo: ProjectServiceRepository) -> None:
        self._repo = repo

    def count_active(self, *, project_id: int) -> int:
        return len(self._repo.list_for_project(project_id=project_id))

    def list_for_project(self, *, project_id: int) -> list[_ServiceRow]:
        return [
            _ServiceRow(id=row.id, name=row.name, description=row.description)
            for row in self._repo.list_for_project(project_id=project_id)
        ]

    def get_by_name(
        self, *, project_id: int, name: str
    ) -> _ServiceRow | None:
        row = self._repo.get_by_name(project_id=project_id, name=name)
        if row is None:
            return None
        return _ServiceRow(id=row.id, name=row.name, description=row.description)


class _StubAvailability:
    def __init__(self, *, slot: AvailabilitySlot | None = None) -> None:
        self._slot = slot

    async def __call__(
        self,
        *,
        project_id: int,
        service_id: int,
        window: tuple[date, date],
        now: datetime,
    ) -> AvailabilitySlot | None:
        return self._slot


class _SettingsRepo:
    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled

    def is_enabled(self, project_id: int) -> bool:
        return self._enabled


class _StubOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx(chat_id: int, project_id: int) -> AnswerContext:
    return AnswerContext(
        chat_id=chat_id,
        customer_username="darya",
        trace_id=f"e2e-12-07-{chat_id}",
        now=_NOW,
        project_id=project_id,
        grounding_threshold=0.6,
    )


def _build(
    tmp_path,
    *,
    enabled: bool,
    slot: AvailabilitySlot | None,
) -> tuple[
    SalesPersonaAnswerer,
    StateRepository,
    ProjectServiceRepository,
    _StubOpenRouter,
]:
    sales_db = str(tmp_path / "sales.sqlite3")
    services_db = str(tmp_path / "services.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    services_repo = ProjectServiceRepository(db_path=services_db)
    openrouter = _StubOpenRouter()
    proposer = DateProposer(
        availability_compute=_StubAvailability(slot=slot),
        services_repo=_InnerServicesRepo(services_repo),
        settings_repo=_SettingsRepo(enabled=enabled),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
    )
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_OuterServicesRepo(services_repo),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        date_proposer=proposer,
    )
    return answerer, state_repo, services_repo, openrouter


def _seed_proposing(state_repo: StateRepository, *, chat_id: int, project_id: int) -> None:
    """Bypass earlier-stage seams: drop straight into ``proposing`` with a date."""
    state_repo.upsert(
        chat_id=chat_id,
        project_id=project_id,
        current_stage=STAGE_PROPOSING,
        collected_intent=Intent(
            dates="1 мая",
            headcount=4,
            vehicle_count=2,
            difficulty="средний",
            drivers="мужчины 30+",
        ).to_dict(),
        last_proposal=None,
        now=_NOW,
    )


@pytest.mark.asyncio
async def test_calendar_enabled_proposes_and_accepts(tmp_path) -> None:
    slot = AvailabilitySlot(
        date=date(2026, 5, 1),
        start_time=time(14, 0),
        end_time=time(15, 0),
    )
    answerer, state_repo, services_repo, openrouter = _build(
        tmp_path, enabled=True, slot=slot
    )
    services_repo.upsert(
        project_id=42,
        name="Каньонинг",
        description="Каньонинг — это спуск по верёвке вдоль водопадов.",
        duration_minutes=60,
    )
    _seed_proposing(state_repo, chat_id=10, project_id=42)

    # Turn A — bot proposes.
    openrouter.queue_response(
        {"text": "Предлагаю на 1 мая с началом в 14:00."}
    )
    propose = await answerer.try_answer(
        question="ну что предложите?",
        ctx=_ctx(chat_id=10, project_id=42),
    )
    assert propose.handled is True
    assert "1 мая" in (propose.text or "")
    assert "14:00" in (propose.text or "")
    assert propose.metadata["proposal"]["date_iso"] == "2026-05-01"

    persisted = state_repo.get(10)
    assert persisted is not None
    assert persisted["current_stage"] == STAGE_PROPOSING
    assert persisted["last_proposal"]["date_iso"] == "2026-05-01"

    # Turn B — customer accepts → closing + HITL handoff.
    accept = await answerer.try_answer(
        question="да, согласен",
        ctx=_ctx(chat_id=10, project_id=42),
    )
    assert accept.handled is True
    assert accept.text == CLOSING_HANDOFF_LINE
    assert accept.metadata["hitl_reason"] == HITL_REASON_CLOSING_HANDOFF
    assert accept.metadata["stage_after"] == STAGE_CLOSING

    closed = state_repo.get(10)
    assert closed is not None
    assert closed["current_stage"] == STAGE_CLOSING


@pytest.mark.asyncio
async def test_calendar_disabled_escalates_with_fixed_line(tmp_path) -> None:
    answerer, state_repo, services_repo, _ = _build(
        tmp_path, enabled=False, slot=None
    )
    services_repo.upsert(
        project_id=42,
        name="Каньонинг",
        description="Описание услуги",
        duration_minutes=60,
    )
    _seed_proposing(state_repo, chat_id=11, project_id=42)

    result = await answerer.try_answer(
        question="ну что предложите?",
        ctx=_ctx(chat_id=11, project_id=42),
    )

    assert result.handled is True
    assert result.text == PROPOSAL_FALLBACK_CALENDAR_DISABLED
    assert result.metadata["hitl_reason"] == HITL_REASON_CALENDAR_DISABLED

    # State stayed in proposing — operator can still resolve the date offline.
    persisted = state_repo.get(11)
    assert persisted is not None
    assert persisted["current_stage"] == STAGE_PROPOSING
