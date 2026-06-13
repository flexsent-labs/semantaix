"""Raw-row retention purge worker (Story 14.05).

Deletes rows from the three raw usage tables that are older than
``retention_days`` days.  Never touches ``usage_daily_summary``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from services.scheduler.app.usage_rollup import RollupRepos

logger = logging.getLogger(__name__)


async def run_retention(
    *,
    clock: Callable[[], datetime],
    repos: RollupRepos,
    retention_days: int = 30,
    batch_size: int = 10_000,
) -> None:
    """Purge raw usage rows older than ``retention_days`` days."""
    cutoff_dt = clock() - timedelta(days=retention_days)
    cutoff_iso = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    logger.info(
        "usage_retention_started",
        extra={"cutoff_iso": cutoff_iso, "retention_days": retention_days},
    )

    for repo, table in (
        (repos.llm, "usage_llm_calls"),
        (repos.messages, "usage_messages"),
        (repos.hitl, "usage_hitl_events"),
    ):
        deleted = repo.purge_before(cutoff_iso, batch_size)
        logger.info(
            "usage_retention_purged",
            extra={"table": table, "rows_deleted": deleted},
        )

    logger.info("usage_retention_completed", extra={"cutoff_iso": cutoff_iso})


__all__ = ["run_retention"]
