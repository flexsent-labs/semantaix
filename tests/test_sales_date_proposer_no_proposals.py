"""``NoProposal`` paths for ``DateProposer.propose`` (Story 12.07).

Every short-circuit returns a :class:`NoProposal` with a stable reason
so the answerer can branch on it deterministically. The four canonical
reasons exercised here:

- ``ambiguous_service`` — zero or more than one active service.
- ``calendar_not_enabled`` — opt-in gate returns False.
- ``provider_error`` — ``availability_compute`` raises
  :class:`CalendarProviderError`.
- ``no_slots_in_window`` — ``availability_compute`` returns ``None``.

Plus ``no_date_hint`` — the intent doesn't carry a parseable date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from services.api.app.calendar.calendar_client import CalendarProviderError
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import (
    NO_PROPOSAL_AMBIGUOUS_SERVICE,
    NO_PROPOSAL_CALENDAR_NOT_ENABLED,
    NO_PROPOSAL_NO_DATE_HINT,
    NO_PROPOSAL_NO_SLOTS_IN_WINDOW,
    NO_PROPOSAL_PROVIDER_ERROR,
    AvailabilitySlot,
    DateProposer,
    NoProposal,
)
from services.api.app.sales.intent import Intent


@dataclass
class _Service:
    id: int
    name: str


class _ServicesRepo:
    def __init__(self, services: list[_Service]) -> None:
        self._services = services

    def list_for_project(self, *, project_id: int) -> list[_Service]:
        return list(self._services)


class _SettingsRepo:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def is_enabled(self, project_id: int) -> bool:
        return self.enabled


class _StubAvailability:
    def __init__(
        self,
        *,
        slot: AvailabilitySlot | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._slot = slot
        self._raises = raises
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
        if self._raises is not None:
            raise self._raises
        return self._slot


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _build_proposer(
    *,
    services: list[_Service],
    enabled: bool = True,
    slot: AvailabilitySlot | None = None,
    raises: Exception | None = None,
) -> tuple[DateProposer, _StubAvailability]:
    stub = _StubAvailability(slot=slot, raises=raises)
    proposer = DateProposer(
        availability_compute=stub,
        services_repo=_ServicesRepo(services),
        settings_repo=_SettingsRepo(enabled=enabled),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
    )
    return proposer, stub


@pytest.mark.asyncio
async def test_calendar_not_enabled_short_circuits_first() -> None:
    proposer, stub = _build_proposer(
        services=[_Service(id=1, name="A")],
        enabled=False,
        slot=AvailabilitySlot(date(2026, 5, 1), time(14, 0), time(15, 0)),
    )
    result = await proposer.propose(
        project_id=7, intent=Intent(dates="1 мая"), now=_NOW
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_CALENDAR_NOT_ENABLED
    # The provider must never be hit when calendar is disabled.
    assert stub.calls == []


@pytest.mark.asyncio
async def test_ambiguous_service_when_two_active() -> None:
    proposer, stub = _build_proposer(
        services=[
            _Service(id=1, name="Каньонинг"),
            _Service(id=2, name="Эндуро"),
        ],
        slot=AvailabilitySlot(date(2026, 5, 1), time(14, 0), time(15, 0)),
    )
    result = await proposer.propose(
        project_id=7, intent=Intent(dates="1 мая"), now=_NOW
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_AMBIGUOUS_SERVICE
    # No provider call when we can't even resolve a service.
    assert stub.calls == []


@pytest.mark.asyncio
async def test_ambiguous_service_when_zero_active() -> None:
    proposer, _ = _build_proposer(services=[], slot=None)
    result = await proposer.propose(
        project_id=7, intent=Intent(dates="1 мая"), now=_NOW
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_AMBIGUOUS_SERVICE


@pytest.mark.asyncio
async def test_provider_error_when_availability_raises() -> None:
    proposer, _ = _build_proposer(
        services=[_Service(id=1, name="Каньонинг")],
        raises=CalendarProviderError("boom"),
    )
    result = await proposer.propose(
        project_id=7, intent=Intent(dates="1 мая"), now=_NOW
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_PROVIDER_ERROR


@pytest.mark.asyncio
async def test_no_slots_in_window_when_availability_returns_none() -> None:
    proposer, _ = _build_proposer(
        services=[_Service(id=1, name="Каньонинг")],
        slot=None,
    )
    result = await proposer.propose(
        project_id=7, intent=Intent(dates="1 мая"), now=_NOW
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_NO_SLOTS_IN_WINDOW


@pytest.mark.asyncio
async def test_no_date_hint_when_intent_dates_unparseable() -> None:
    proposer, stub = _build_proposer(
        services=[_Service(id=1, name="Каньонинг")],
        slot=AvailabilitySlot(date(2026, 5, 1), time(14, 0), time(15, 0)),
    )
    result = await proposer.propose(
        project_id=7,
        intent=Intent(dates="скоро поедем"),
        now=_NOW,
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_NO_DATE_HINT
    assert stub.calls == []


@pytest.mark.asyncio
async def test_no_date_hint_when_intent_dates_missing() -> None:
    proposer, _ = _build_proposer(
        services=[_Service(id=1, name="Каньонинг")],
        slot=AvailabilitySlot(date(2026, 5, 1), time(14, 0), time(15, 0)),
    )
    result = await proposer.propose(
        project_id=7, intent=Intent(), now=_NOW
    )
    assert isinstance(result, NoProposal)
    assert result.reason == NO_PROPOSAL_NO_DATE_HINT
