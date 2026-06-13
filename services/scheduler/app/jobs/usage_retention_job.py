"""UsageRetentionJob — raw-row retention purge scheduler job (Story 14.05)."""
from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from typing import Callable

from services.scheduler.app.usage_retention import run_retention
from services.scheduler.app.usage_rollup import RollupRepos

logger = logging.getLogger(__name__)

_STATE_KEY = "last_retention_date_utc"
_DUE_MINUTE_OFFSET = 5


class UsageRetentionJob:
    name = "usage_retention"

    def __init__(
        self,
        *,
        repos: RollupRepos,
        clock: Callable[[], datetime],
        state_path: str,
        rollup_hour_utc: int = 0,
        retention_days: int = 30,
        batch_size: int = 10_000,
    ) -> None:
        self._repos = repos
        self._clock = clock
        self._state_path = state_path
        self._rollup_hour_utc = rollup_hour_utc
        self._retention_days = retention_days
        self._batch_size = batch_size
        self._last_run_date: date | None = self._load_state()

    def _load_state(self) -> date | None:
        try:
            with open(self._state_path) as f:
                data = json.load(f)
            val = data.get(_STATE_KEY)
            return date.fromisoformat(val) if val else None
        except (FileNotFoundError, json.JSONDecodeError, ValueError):
            return None

    def _save_state(self, run_date: date) -> None:
        try:
            with open(self._state_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}
        data[_STATE_KEY] = run_date.isoformat()
        with open(self._state_path, "w") as f:
            json.dump(data, f)
        self._last_run_date = run_date

    def _is_due(self, now: datetime) -> bool:
        today_utc = now.date()
        due_time = datetime(
            today_utc.year, today_utc.month, today_utc.day,
            self._rollup_hour_utc, _DUE_MINUTE_OFFSET, 0, tzinfo=UTC,
        )
        if now < due_time:
            return False
        return self._last_run_date is None or self._last_run_date < today_utc

    async def run(self) -> None:
        now = self._clock()
        if not self._is_due(now):
            return
        logger.info("usage_retention_job_running")
        await run_retention(
            clock=self._clock,
            repos=self._repos,
            retention_days=self._retention_days,
            batch_size=self._batch_size,
        )
        self._save_state(now.date())


__all__ = ["UsageRetentionJob"]
