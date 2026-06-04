"""``check_requested_availability`` — "is THIS exact slot free?" helper.

Factored out of ``CalendarAvailabilityAnswerer._compute_answer`` so the
sales completion handler can validate a customer's *specific* requested
time (e.g. "завтра в 14:00") against the calendar, rather than only
proposing the next free slot. The caller resolves the ``service_rule`` and
the routing operator; this helper owns the connectivity gate, the single
``query_busy`` call, and the pure ``compute_availability`` verdict.

Returns a small :class:`RequestedAvailability` status object (never raises
for calendar-backend failures — those map to ``status="error"``), so the
caller decides the customer-facing behaviour (propose an alternative,
confirm, or escalate).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from services.api.app.calendar.access_token_cache import CalendarReconnectNeeded
from services.api.app.calendar.availability import (
    compute_availability,
    find_earliest_slot,
    parse_service_rule,
)
from services.api.app.calendar.calendar_client import CalendarProviderError
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.calendar.token_repository import TokenNotFound

logger = logging.getLogger(__name__)

STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_CONNECTED = "not_connected"
STATUS_ERROR = "error"

# Story 12.71 (round-17 R17-4) — the generic ``lookahead_days`` bounds the
# "find me a slot" scan; it must NOT make a customer's *specifically named*
# future date read as "busy". When the requested date sits beyond the generic
# horizon we widen the freebusy window (and the availability horizon) to cover
# it, up to this defensive cap so a "1 января 2099" can't ask the backend for a
# decades-long window. Beyond the cap the slot stays ``outside_lookahead``.
_REQUESTED_LOOKAHEAD_CAP_DAYS = 400

ERROR_RECONNECT_NEEDED = "reconnect_needed"
ERROR_TOKEN_NOT_FOUND = "token_not_found"
ERROR_PROVIDER_ERROR = "provider_error"


class _TokenProvider(Protocol):
    async def get_access_token(
        self,
        project_id: int,
        operator: str,
        *,
        operator_chat_id: int,
        trace_id: str,
    ) -> str: ...


class _FreeBusyClient(Protocol):
    async def query_busy(
        self,
        *,
        access_token: str,
        calendar_id: str = "primary",
        time_min: Any,
        time_max: Any,
        trace_id: str,
    ) -> Any: ...


@dataclass(frozen=True)
class RequestedAvailability:
    """Verdict for a specific requested slot.

    ``status`` is one of the ``STATUS_*`` constants. ``reason`` carries the
    ``compute_availability`` reason for ``unavailable`` (e.g. ``"busy"``) or
    the ``ERROR_*`` reason for ``error``; it is ``None`` for ``available``
    and ``not_connected``. ``alternative`` is the nearest free start instant
    (project-tz aware) when the requested time is ``unavailable`` and a free
    slot exists within the look-ahead horizon — ``None`` otherwise.
    """

    status: str
    reason: str | None = None
    alternative: datetime | None = None


async def check_requested_availability(
    *,
    project_id: int,
    requested_start: datetime,
    operator: str | None,
    operator_chat_id: int | None,
    service_rule: ServiceRule,
    token_provider: _TokenProvider | None,
    freebusy_client: _FreeBusyClient | None,
    now: datetime,
    project_tz: ZoneInfo,
    lookahead_days: int,
    country_code: str,
    trace_id: str,
) -> RequestedAvailability:
    """Check whether ``requested_start`` is free on the operator's calendar."""
    if (
        token_provider is None
        or freebusy_client is None
        or not operator
        or operator_chat_id is None
    ):
        return RequestedAvailability(status=STATUS_NOT_CONNECTED)

    # Widen the effective horizon to cover a specifically-named far-future date
    # (Story 12.71, R17-4), capped so the freebusy query window stays bounded.
    days_to_requested = (
        requested_start.astimezone(project_tz).date()
        - now.astimezone(project_tz).date()
    ).days
    effective_lookahead = max(
        lookahead_days,
        min(days_to_requested + 1, _REQUESTED_LOOKAHEAD_CAP_DAYS),
    )

    try:
        access_token = await token_provider.get_access_token(
            project_id,
            operator,
            operator_chat_id=operator_chat_id,
            trace_id=trace_id,
        )
        time_min = now
        time_max = now + timedelta(days=effective_lookahead)
        free_busy = await freebusy_client.query_busy(
            access_token=access_token,
            time_min=time_min,
            time_max=time_max,
            trace_id=trace_id,
        )
    except CalendarReconnectNeeded:
        return RequestedAvailability(
            status=STATUS_ERROR, reason=ERROR_RECONNECT_NEEDED
        )
    except TokenNotFound:
        return RequestedAvailability(
            status=STATUS_ERROR, reason=ERROR_TOKEN_NOT_FOUND
        )
    except CalendarProviderError:
        return RequestedAvailability(
            status=STATUS_ERROR, reason=ERROR_PROVIDER_ERROR
        )

    parsed_rule = parse_service_rule(
        service_rule,
        lookahead_days=effective_lookahead,
        country_code=country_code,
    )
    result = compute_availability(
        now=now,
        requested_start=requested_start,
        busy=free_busy.busy,
        service_rule=parsed_rule,
        project_tz=project_tz,
    )
    logger.info(
        "sales_requested_time_checked",
        extra={
            "trace_id": trace_id,
            "available": result.available,
            "reason": result.reason,
            "busy_blocks": len(free_busy.busy),
        },
    )
    if result.available:
        return RequestedAvailability(status=STATUS_AVAILABLE)

    # Unavailable — offer the nearest free slot from the requested day forward.
    requested_date = requested_start.astimezone(project_tz).date()
    horizon_date = (
        now.astimezone(project_tz) + timedelta(days=effective_lookahead)
    ).date()
    alternative = find_earliest_slot(
        now=now,
        window=(requested_date, horizon_date),
        busy=free_busy.busy,
        service_rule=parsed_rule,
        project_tz=project_tz,
    )
    return RequestedAvailability(
        status=STATUS_UNAVAILABLE, reason=result.reason, alternative=alternative
    )


__all__ = [
    "ERROR_PROVIDER_ERROR",
    "ERROR_RECONNECT_NEEDED",
    "ERROR_TOKEN_NOT_FOUND",
    "RequestedAvailability",
    "STATUS_AVAILABLE",
    "STATUS_ERROR",
    "STATUS_NOT_CONNECTED",
    "STATUS_UNAVAILABLE",
    "check_requested_availability",
]
