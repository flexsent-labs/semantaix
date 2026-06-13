"""Daily usage roll-up worker (Story 14.05).

Aggregates raw usage rows into ``usage_daily_summary`` for every elapsed UTC
day, capped at a 30-day look-back window.  Idempotent — re-running a day
overwrites its summary row with the same aggregated values.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageDailySummaryRow,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)

logger = logging.getLogger(__name__)

_LOOKBACK_DAYS = 30


@dataclass
class RollupRepos:
    llm: UsageLlmCallRepository
    messages: UsageMessageRepository
    hitl: UsageHitlEventRepository
    summary: UsageDailySummaryRepository
    db_path: str


def _discover_project_ids(db_path: str) -> list[int]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT project_id FROM usage_llm_calls"
            " UNION"
            " SELECT DISTINCT project_id FROM usage_messages"
            " UNION"
            " SELECT DISTINCT project_id FROM usage_hitl_events"
        ).fetchall()
    return [r[0] for r in rows]


def _last_summary_day(db_path: str, project_id: int) -> date | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT MAX(day_utc) FROM usage_daily_summary WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return None


def _aggregate_llm(
    db_path: str, project_id: int, day: date
) -> list[UsageDailySummaryRow]:
    day_str = day.isoformat()
    start_ts = f"{day_str}T00:00:00Z"
    end_ts = f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT
                model_name,
                SUM(prompt_tokens)                       AS prompt_tokens_total,
                SUM(completion_tokens)                   AS completion_tokens_total,
                SUM(COALESCE(cost_usd, 0))               AS cost_usd_total,
                SUM(CASE WHEN call_outcome != 'customer_visible_answer'
                         THEN COALESCE(cost_usd, 0) ELSE 0 END) AS wasted_cost_usd,
                COUNT(*)                                 AS call_count
            FROM usage_llm_calls
            WHERE project_id = ?
              AND created_at >= ? AND created_at < ?
            GROUP BY model_name
            """,
            (project_id, start_ts, end_ts),
        ).fetchall()
    return [
        UsageDailySummaryRow(
            project_id=project_id,
            day_utc=day_str,
            tracker_type="llm",
            model_name=r["model_name"],
            prompt_tokens_total=r["prompt_tokens_total"],
            completion_tokens_total=r["completion_tokens_total"],
            cost_usd_total=r["cost_usd_total"],
            wasted_cost_usd=r["wasted_cost_usd"],
            call_count=r["call_count"],
            in_count=None,
            out_count=None,
            hitl_created_count=None,
            hitl_assigned_count=None,
            hitl_replied_count=None,
            hitl_resolved_count=None,
        )
        for r in rows
    ]


def _aggregate_messages(
    db_path: str, project_id: int, day: date
) -> UsageDailySummaryRow | None:
    day_str = day.isoformat()
    start_ts = f"{day_str}T00:00:00Z"
    end_ts = f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN direction = 'in'  THEN 1 ELSE 0 END) AS in_count,
                SUM(CASE WHEN direction = 'out' THEN 1 ELSE 0 END) AS out_count,
                COUNT(*) AS call_count
            FROM usage_messages
            WHERE project_id = ?
              AND created_at >= ? AND created_at < ?
            """,
            (project_id, start_ts, end_ts),
        ).fetchone()
    if not row or not row["call_count"]:
        return None
    return UsageDailySummaryRow(
        project_id=project_id,
        day_utc=day_str,
        tracker_type="messages",
        model_name="",
        prompt_tokens_total=None,
        completion_tokens_total=None,
        cost_usd_total=None,
        wasted_cost_usd=None,
        call_count=row["call_count"],
        in_count=row["in_count"],
        out_count=row["out_count"],
        hitl_created_count=None,
        hitl_assigned_count=None,
        hitl_replied_count=None,
        hitl_resolved_count=None,
    )


def _aggregate_hitl(
    db_path: str, project_id: int, day: date
) -> UsageDailySummaryRow | None:
    day_str = day.isoformat()
    start_ts = f"{day_str}T00:00:00Z"
    end_ts = f"{(day + timedelta(days=1)).isoformat()}T00:00:00Z"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT
                SUM(CASE WHEN event_type = 'created'  THEN 1 ELSE 0 END) AS hitl_created_count,
                SUM(CASE WHEN event_type = 'assigned' THEN 1 ELSE 0 END) AS hitl_assigned_count,
                SUM(CASE WHEN event_type = 'replied'  THEN 1 ELSE 0 END) AS hitl_replied_count,
                SUM(CASE WHEN event_type = 'resolved' THEN 1 ELSE 0 END) AS hitl_resolved_count,
                COUNT(*) AS call_count
            FROM usage_hitl_events
            WHERE project_id = ?
              AND created_at >= ? AND created_at < ?
            """,
            (project_id, start_ts, end_ts),
        ).fetchone()
    if not row or not row["call_count"]:
        return None
    return UsageDailySummaryRow(
        project_id=project_id,
        day_utc=day_str,
        tracker_type="hitl",
        model_name="",
        prompt_tokens_total=None,
        completion_tokens_total=None,
        cost_usd_total=None,
        wasted_cost_usd=None,
        call_count=row["call_count"],
        in_count=None,
        out_count=None,
        hitl_created_count=row["hitl_created_count"],
        hitl_assigned_count=row["hitl_assigned_count"],
        hitl_replied_count=row["hitl_replied_count"],
        hitl_resolved_count=row["hitl_resolved_count"],
    )


async def run_rollup(
    *,
    clock: Callable[[], datetime],
    repos: RollupRepos,
) -> None:
    """Roll up raw usage rows into daily summaries for all elapsed days."""
    today = clock().date()
    yesterday = today - timedelta(days=1)
    earliest = today - timedelta(days=_LOOKBACK_DAYS)

    logger.info("usage_rollup_started", extra={"today": today.isoformat()})

    project_ids = _discover_project_ids(repos.db_path)
    for project_id in project_ids:
        last_day = _last_summary_day(repos.db_path, project_id)
        if last_day is None:
            start_day = earliest
        else:
            start_day = max(last_day + timedelta(days=1), earliest)

        if start_day > yesterday:
            continue

        current = start_day
        while current <= yesterday:
            for row in _aggregate_llm(repos.db_path, project_id, current):
                repos.summary.upsert(row)

            msg_row = _aggregate_messages(repos.db_path, project_id, current)
            if msg_row is not None:
                repos.summary.upsert(msg_row)

            hitl_row = _aggregate_hitl(repos.db_path, project_id, current)
            if hitl_row is not None:
                repos.summary.upsert(hitl_row)

            logger.debug(
                "usage_rollup_day_completed",
                extra={"project_id": project_id, "day": current.isoformat()},
            )
            current += timedelta(days=1)

    logger.info("usage_rollup_completed", extra={"today": today.isoformat()})


__all__ = ["RollupRepos", "run_rollup"]
