"""Happy-path test for ``DateProposer.propose`` (Story 12.07).

When the project has a single active service, calendar is enabled, and
``availability_compute`` returns a slot inside the parsed window, the
proposer emits a :class:`Proposal` with the slot's verbatim values.
The proposer itself never persists state — that is the answerer's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import (
    SLOT_SOURCE_EPIC11,
    AvailabilitySlot,
    DateProposer,
    Proposal,
)
from services.api.app.sales.intent import Intent


@dataclass
class _Service:
    id: int
    name: str


class _ServicesRepo:
    def __init__(self, services: list[_Service]) -> None:
        self._services = services
        self.list_calls: list[int] = []

    def list_for_project(self, *, project_id: int) -> list[_Service]:
        self.list_calls.append(project_id)
        return list(self._services)


class _SettingsRepo:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.is_enabled_calls: list[int] = []

    def is_enabled(self, project_id: int) -> bool:
        self.is_enabled_calls.append(project_id)
        return self.enabled


class _StubAvailability:
    def __init__(self, slot: AvailabilitySlot | None) -> None:
        self._slot = slot
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        project_id: int,
        service_id: int,
        window: tuple[date, date],
        now: datetime,
    ) -> AvailabilitySlot | None:
        self.calls.append(
            {
                "project_id": project_id,
                "service_id": service_id,
                "window": window,
                "now": now,
            }
        )
        return self._slot


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _build_proposer(
    *,
    services: list[_Service],
    enabled: bool = True,
    slot: AvailabilitySlot | None = None,
) -> tuple[DateProposer, _StubAvailability, _SettingsRepo, _ServicesRepo]:
    stub = _StubAvailability(slot=slot)
    settings_repo = _SettingsRepo(enabled=enabled)
    services_repo = _ServicesRepo(services=services)
    proposer = DateProposer(
        availability_compute=stub,
        services_repo=services_repo,
        settings_repo=settings_repo,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
    )
    return proposer, stub, settings_repo, services_repo


@pytest.mark.asyncio
async def test_propose_returns_proposal_when_slot_available() -> None:
    slot = AvailabilitySlot(
        date=date(2026, 5, 1),
        start_time=time(14, 0),
        end_time=time(15, 0),
    )
    proposer, stub, _, _ = _build_proposer(
        services=[_Service(id=42, name="Каньонинг")],
        slot=slot,
    )
    intent = Intent(dates="1 мая")

    result = await proposer.propose(project_id=7, intent=intent, now=_NOW)

    assert isinstance(result, Proposal)
    assert result.date_iso == "2026-05-01"
    assert result.start_time_iso == "14:00"
    assert result.end_time_iso == "15:00"
    assert result.service_id == 42
    assert result.slot_source == SLOT_SOURCE_EPIC11
    # proposed_at is an ISO-8601 UTC timestamp from ``now``.
    assert result.proposed_at == "2026-04-25T09:00:00+00:00"

    # Single availability call with the parsed window.
    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["project_id"] == 7
    assert call["service_id"] == 42
    assert call["window"] == (date(2026, 5, 1), date(2026, 5, 1))
    assert call["now"] == _NOW


@pytest.mark.asyncio
async def test_proposal_as_dict_round_trip_includes_proposed_at() -> None:
    slot = AvailabilitySlot(
        date=date(2026, 5, 1),
        start_time=time(14, 0),
        end_time=time(15, 0),
    )
    proposer, _, _, _ = _build_proposer(
        services=[_Service(id=42, name="Каньонинг")],
        slot=slot,
    )
    intent = Intent(dates="1 мая")

    result = await proposer.propose(project_id=7, intent=intent, now=_NOW)
    assert isinstance(result, Proposal)

    encoded = result.as_dict()
    assert encoded["date_iso"] == "2026-05-01"
    assert encoded["start_time_iso"] == "14:00"
    assert encoded["end_time_iso"] == "15:00"
    assert encoded["service_id"] == 42
    assert encoded["slot_source"] == SLOT_SOURCE_EPIC11
    assert encoded["proposed_at"] == "2026-04-25T09:00:00+00:00"


@pytest.mark.asyncio
async def test_propose_passes_range_window_when_intent_has_range_dates() -> None:
    slot = AvailabilitySlot(
        date=date(2026, 5, 2),
        start_time=time(10, 0),
        end_time=time(11, 0),
    )
    proposer, stub, _, _ = _build_proposer(
        services=[_Service(id=99, name="Эндуро")],
        slot=slot,
    )
    intent = Intent(dates="1–3 мая")

    result = await proposer.propose(project_id=1, intent=intent, now=_NOW)

    assert isinstance(result, Proposal)
    assert stub.calls[0]["window"] == (date(2026, 5, 1), date(2026, 5, 3))


@pytest.mark.asyncio
async def test_propose_uses_only_active_service_when_one_exists() -> None:
    slot = AvailabilitySlot(
        date=date(2026, 5, 1),
        start_time=time(14, 0),
        end_time=time(15, 0),
    )
    only_service = _Service(id=1, name="Каньонинг")
    proposer, stub, _, _ = _build_proposer(
        services=[only_service], slot=slot
    )

    result = await proposer.propose(
        project_id=1, intent=Intent(dates="1 мая"), now=_NOW
    )

    assert isinstance(result, Proposal)
    assert result.service_id == 1
    assert stub.calls[0]["service_id"] == 1
