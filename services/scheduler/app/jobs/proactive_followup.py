"""Proactive +1d follow-up job (Story 12.08).

Each scheduler tick the job:
  1. Pulls due rows from ``GET /sales/followups/due``.
  2. For each row, asks the api to do one of three things:
     * ``skip-stale`` when the customer's stated tour date already lapsed
       (decided locally — the api does not load conversation state for the
       scheduler).
     * ``reschedule`` to today's 10:00 (project tz) when ``now`` lies in
       the quiet-hours window 21:00 – 10:00.
     * ``fire`` (the api renders + sends the Telegram nudge).

The job is single-process — no distributed locking, no retries beyond
"try again on the next tick". It owns no state itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from services.api.app.sales.russian_dates import parse_russian_date_span


def timezone_utc() -> tzinfo:
    return UTC

logger = logging.getLogger(__name__)

QUIET_HOURS_START = time(21, 0)
QUIET_HOURS_END = time(10, 0)


@dataclass
class JobResult:
    fired: int = 0
    skipped_stale: int = 0
    rescheduled: int = 0
    errors: list[str] = field(default_factory=list)

    def total_processed(self) -> int:
        return self.fired + self.skipped_stale + self.rescheduled


class _ApiClient(Protocol):
    async def list_due_followups(
        self, *, now: datetime
    ) -> list[dict[str, Any]]: ...

    async def skip_stale(self, followup_id: int) -> None: ...

    async def reschedule(
        self, followup_id: int, *, new_fire_at: datetime
    ) -> None: ...

    async def fire(self, followup_id: int) -> dict[str, Any]: ...


def in_quiet_hours(now_local: datetime) -> bool:
    """Return ``True`` when ``now_local`` lies in [21:00, 10:00) project tz.

    The window wraps midnight, so the test is "after start OR before end".
    """
    t = now_local.time()
    return t >= QUIET_HOURS_START or t < QUIET_HOURS_END


def next_morning_local(now_local: datetime) -> datetime:
    """Earliest 10:00 in the project tz after ``now_local``.

    If the customer's local clock is before 10:00, the next 10:00 is the
    same calendar day; if it's after, the next 10:00 is tomorrow.
    """
    target = now_local.replace(
        hour=QUIET_HOURS_END.hour, minute=0, second=0, microsecond=0
    )
    if now_local >= target:
        target = target + timedelta(days=1)
    return target


class ProactiveFollowupJob:
    name = "proactive_followup"

    def __init__(
        self,
        *,
        api_client: _ApiClient,
        clock: Callable[[], datetime],
        project_tz_lookup: Callable[[int], str],
    ) -> None:
        self._api = api_client
        self._clock = clock
        self._tz_lookup = project_tz_lookup

    async def run(self) -> JobResult:
        result = JobResult()
        now = self._clock()
        try:
            rows = await self._api.list_due_followups(now=now)
        except Exception as exc:  # broad: external transport / parse
            logger.warning("proactive_followup_due_failed", extra={"error": repr(exc)})
            result.errors.append(f"due:{exc!r}")
            return result

        for raw in rows:
            try:
                await self._handle_row(raw, result=result, now=now)
            except Exception as exc:  # broad: never let one row poison the tick
                logger.warning(
                    "proactive_followup_row_failed",
                    extra={"row": raw, "error": repr(exc)},
                )
                result.errors.append(f"row:{exc!r}")
        return result

    async def _handle_row(
        self,
        raw: dict[str, Any],
        *,
        result: JobResult,
        now: datetime,
    ) -> None:
        followup_id = int(raw["id"])
        project_id = int(raw["project_id"])
        intent_dates = raw.get("intent_dates")
        if isinstance(intent_dates, str):
            intent_dates_str: str | None = intent_dates
        else:
            intent_dates_str = None

        tz = ZoneInfo(self._tz_lookup(project_id))
        now_local = now.astimezone(tz)
        intent_date = parse_russian_date_span(
            intent_dates_str, today=now_local.date()
        )
        if intent_date is not None and intent_date < now_local.date():
            await self._api.skip_stale(followup_id)
            result.skipped_stale += 1
            logger.info(
                "proactive_followup_skip_stale",
                extra={
                    "followup_id": followup_id,
                    "intent_date": intent_date.isoformat(),
                    "today_local": now_local.date().isoformat(),
                },
            )
            return

        if in_quiet_hours(now_local):
            target_local = next_morning_local(now_local)
            target_utc = target_local.astimezone(timezone_utc())
            await self._api.reschedule(followup_id, new_fire_at=target_utc)
            result.rescheduled += 1
            logger.info(
                "proactive_followup_reschedule",
                extra={
                    "followup_id": followup_id,
                    "now_local": now_local.isoformat(),
                    "target_local": target_local.isoformat(),
                },
            )
            return

        fire_response = await self._api.fire(followup_id)
        result.fired += 1
        logger.info(
            "proactive_followup_fire",
            extra={
                "followup_id": followup_id,
                "response": fire_response,
            },
        )


__all__ = [
    "JobResult",
    "ProactiveFollowupJob",
    "QUIET_HOURS_END",
    "QUIET_HOURS_START",
    "in_quiet_hours",
    "next_morning_local",
    "timezone_utc",
]
