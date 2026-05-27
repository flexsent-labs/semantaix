"""``DateProposer`` — earliest-available-slot lookup for the ``proposing`` stage.

The proposer is a thin pure-Python composition layer between the sales
funnel and Epic 11's calendar availability engine. It never asks an LLM
for a date; the only datetime the customer ever sees is the one
``availability_compute`` returned for a concrete ``(project_id,
service_id, window)`` query.

Inputs (all injected):
- ``availability_compute`` — async callable wrapping Epic-11's pure
  ``compute_availability`` plus its busy-block retrieval. Returns an
  :class:`AvailabilitySlot` (free slot) or ``None`` (no slot in window).
  Raises :class:`CalendarProviderError` when the calendar backend is
  unreachable — the proposer translates that into
  ``NoProposal(reason="provider_error")``.
- ``services_repo`` — typed-row catalog of project services. The proposer
  uses the only-active-service shortcut (FR-23 mid-funnel: ambiguous
  services aren't auto-picked).
- ``settings_repo`` — calendar opt-in tri-state. The proposer hits
  ``is_enabled`` FIRST so a disabled project takes the
  ``calendar_not_enabled`` branch without ever touching the catalog or
  the calendar provider.

Outputs are frozen dataclasses: :class:`Proposal` or :class:`NoProposal`.
``Proposal.as_dict()`` is the canonical shape persisted into
``state.last_proposal`` (the answerer is the one that calls
``state_repo.upsert``).
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Protocol

from services.api.app.calendar.calendar_client import CalendarProviderError
from services.api.app.sales.date_parser import parse_russian_date_span
from services.api.app.sales.intent import Intent

logger = logging.getLogger(__name__)

SLOT_SOURCE_EPIC11 = "epic11_availability"

NO_PROPOSAL_AMBIGUOUS_SERVICE = "ambiguous_service"
NO_PROPOSAL_CALENDAR_NOT_ENABLED = "calendar_not_enabled"
NO_PROPOSAL_PROVIDER_ERROR = "provider_error"
NO_PROPOSAL_NO_SLOTS_IN_WINDOW = "no_slots_in_window"
NO_PROPOSAL_NO_DATE_HINT = "no_date_hint"


@dataclass(frozen=True)
class AvailabilitySlot:
    """Concrete free slot returned by ``availability_compute``."""

    date: date
    start_time: time
    end_time: time


@dataclass(frozen=True)
class Proposal:
    """A concrete, verified date proposal for the customer."""

    date_iso: str
    start_time_iso: str
    end_time_iso: str
    service_id: int
    slot_source: str = SLOT_SOURCE_EPIC11
    proposed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class NoProposal:
    """Why the proposer could not produce a slot for this turn."""

    reason: str


class _Service(Protocol):
    id: int
    name: str


class _ServicesRepo(Protocol):
    def list_for_project(self, *, project_id: int) -> list[_Service]: ...


class _SettingsRepo(Protocol):
    def is_enabled(self, project_id: int) -> bool: ...


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


class _AvailabilityCompute(Protocol):
    async def __call__(
        self,
        *,
        project_id: int,
        service_id: int,
        window: tuple[date, date],
        now: datetime,
    ) -> AvailabilitySlot | None: ...


class DateProposer:
    """Resolve the next concrete slot for the ``proposing`` stage."""

    def __init__(
        self,
        *,
        availability_compute: _AvailabilityCompute,
        services_repo: _ServicesRepo,
        settings_repo: _SettingsRepo,
        normalizer: _Normalizer,
        clock,
    ) -> None:
        self._availability_compute = availability_compute
        self._services_repo = services_repo
        self._settings_repo = settings_repo
        self._normalizer = normalizer
        self._clock = clock

    async def propose(
        self,
        *,
        project_id: int,
        intent: Intent,
        now: datetime,
    ) -> Proposal | NoProposal:
        if not self._settings_repo.is_enabled(project_id):
            return NoProposal(reason=NO_PROPOSAL_CALENDAR_NOT_ENABLED)

        services = self._services_repo.list_for_project(project_id=project_id)
        active = [
            service
            for service in services
            if getattr(service, "name", None)
        ]
        if len(active) == 0:
            return NoProposal(reason=NO_PROPOSAL_AMBIGUOUS_SERVICE)
        if len(active) > 1:
            return NoProposal(reason=NO_PROPOSAL_AMBIGUOUS_SERVICE)
        service = active[0]

        dates_text = intent.dates if isinstance(intent.dates, str) else None
        window = parse_russian_date_span(dates_text, now=now.date())
        if window is None:
            return NoProposal(reason=NO_PROPOSAL_NO_DATE_HINT)

        try:
            slot = await self._availability_compute(
                project_id=project_id,
                service_id=service.id,
                window=window,
                now=now,
            )
        except CalendarProviderError as exc:
            logger.info(
                "sales_date_proposer_provider_error",
                extra={
                    "project_id": project_id,
                    "service_id": service.id,
                    "error": repr(exc),
                },
            )
            return NoProposal(reason=NO_PROPOSAL_PROVIDER_ERROR)

        if slot is None:
            return NoProposal(reason=NO_PROPOSAL_NO_SLOTS_IN_WINDOW)

        proposed_at = now.astimezone(UTC).isoformat()
        return Proposal(
            date_iso=slot.date.isoformat(),
            start_time_iso=slot.start_time.strftime("%H:%M"),
            end_time_iso=slot.end_time.strftime("%H:%M"),
            service_id=int(service.id),
            slot_source=SLOT_SOURCE_EPIC11,
            proposed_at=proposed_at,
        )


__all__ = [
    "AvailabilitySlot",
    "DateProposer",
    "NO_PROPOSAL_AMBIGUOUS_SERVICE",
    "NO_PROPOSAL_CALENDAR_NOT_ENABLED",
    "NO_PROPOSAL_NO_DATE_HINT",
    "NO_PROPOSAL_NO_SLOTS_IN_WINDOW",
    "NO_PROPOSAL_PROVIDER_ERROR",
    "NoProposal",
    "Proposal",
    "SLOT_SOURCE_EPIC11",
]
