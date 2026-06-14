"""Usage repository skeletons and row types (Story 14.01).

Each repository owns one ``semantaix_usage.db`` table.  Methods raise
``NotImplementedError`` until the indicated story implements them.

Sync ``sqlite3`` per project convention; callers dispatch via
``asyncio.to_thread``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

CALL_OUTCOMES: frozenset[str] = frozenset({
    "customer_visible_answer",
    "verifier_rejected",
    "escalated_to_hitl",
    "guardrails_blocked",
    "moderation_triggered",
    "error",
})

HITL_EVENT_TYPES: frozenset[str] = frozenset({
    "created", "assigned", "replied", "resolved"
})

DIRECTIONS: frozenset[str] = frozenset({"in", "out"})
PARTICIPANT_ROLES: frozenset[str] = frozenset({"customer", "operator"})

# ---------------------------------------------------------------------------
# Frozen row types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageLlmCallRow:
    id: int
    project_id: int
    model_name: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    call_outcome: str
    trace_id: str | None
    created_at: str  # UTC ISO-8601 with Z suffix


@dataclass(frozen=True)
class UsageMessageRow:
    id: int
    project_id: int
    direction: str          # 'in' | 'out'
    participant_role: str   # 'customer' | 'operator'
    trace_id: str | None
    created_at: str


@dataclass(frozen=True)
class UsageHitlEventRow:
    id: int
    project_id: int
    event_type: str   # 'created' | 'assigned' | 'replied' | 'resolved'
    ticket_id: int
    trace_id: str | None
    created_at: str


@dataclass(frozen=True)
class UsageDailySummaryRow:
    project_id: int
    day_utc: str        # YYYY-MM-DD
    tracker_type: str   # 'llm' | 'messages' | 'hitl'
    model_name: str     # empty-string sentinel for non-LLM rows
    prompt_tokens_total: int | None
    completion_tokens_total: int | None
    cost_usd_total: float | None
    wasted_cost_usd: float | None
    call_count: int | None
    in_count: int | None
    out_count: int | None
    hitl_created_count: int | None
    hitl_assigned_count: int | None
    hitl_replied_count: int | None
    hitl_resolved_count: int | None


@dataclass(frozen=True)
class UsageIncidentRow:
    id: int
    project_id: int
    started_at: str
    ended_at: str | None
    breached_trackers: str  # JSON array
    peak_pct: float | None
    total_excess_cost_usd: float | None


# ---------------------------------------------------------------------------
# Repository skeletons
# ---------------------------------------------------------------------------

class UsageLlmCallRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def record(self, row: UsageLlmCallRow) -> None:
        if row.call_outcome not in CALL_OUTCOMES:
            raise ValueError(f"Invalid call_outcome: {row.call_outcome!r}")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO usage_llm_calls"
                " (project_id, model_name, prompt_tokens, completion_tokens,"
                "  cost_usd, call_outcome, trace_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.project_id, row.model_name, row.prompt_tokens,
                    row.completion_tokens, row.cost_usd, row.call_outcome,
                    row.trace_id, row.created_at,
                ),
            )

    def list_for_day(
        self,
        *,
        project_id: int,
        day_utc: str,
        page: int = 1,
        page_size: int = 100,
        include_money: bool = True,
    ) -> list[UsageLlmCallRow]:
        start_ts = f"{day_utc}T00:00:00Z"
        end_ts = f"{(date.fromisoformat(day_utc) + timedelta(days=1)).isoformat()}T00:00:00Z"
        offset = (page - 1) * page_size
        money_col = ", cost_usd" if include_money else ""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, project_id, model_name, prompt_tokens, completion_tokens"
                f"{money_col}, call_outcome, trace_id, created_at"
                " FROM usage_llm_calls"
                " WHERE project_id = ? AND created_at >= ? AND created_at < ?"
                " ORDER BY created_at LIMIT ? OFFSET ?",
                (project_id, start_ts, end_ts, page_size, offset),
            ).fetchall()
        return [
            UsageLlmCallRow(
                id=r["id"],
                project_id=r["project_id"],
                model_name=r["model_name"],
                prompt_tokens=r["prompt_tokens"],
                completion_tokens=r["completion_tokens"],
                cost_usd=r["cost_usd"] if include_money else None,
                call_outcome=r["call_outcome"],
                trace_id=r["trace_id"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def purge_before(self, cutoff_iso: str, batch_size: int = 10_000) -> int:
        deleted = 0
        while True:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    "DELETE FROM usage_llm_calls WHERE id IN"
                    " (SELECT id FROM usage_llm_calls WHERE created_at < ? LIMIT ?)",
                    (cutoff_iso, batch_size),
                )
                n = cur.rowcount
            deleted += n
            if n < batch_size:
                break
        return deleted


class UsageMessageRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def record(self, row: UsageMessageRow) -> None:
        if row.direction not in DIRECTIONS:
            raise ValueError(f"Invalid direction: {row.direction!r}")
        if row.participant_role not in PARTICIPANT_ROLES:
            raise ValueError(f"Invalid participant_role: {row.participant_role!r}")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO usage_messages"
                " (project_id, direction, participant_role, trace_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    row.project_id, row.direction, row.participant_role,
                    row.trace_id, row.created_at,
                ),
            )

    def count_for_day(self, *, project_id: int, day_utc: str) -> int:
        """Implemented in Story 14.05 (roll-up reads raw rows)."""
        raise NotImplementedError

    def list_for_day(
        self, *, project_id: int, day_utc: str, page: int = 1, page_size: int = 100
    ) -> list[UsageMessageRow]:
        start_ts = f"{day_utc}T00:00:00Z"
        end_ts = f"{(date.fromisoformat(day_utc) + timedelta(days=1)).isoformat()}T00:00:00Z"
        offset = (page - 1) * page_size
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, project_id, direction, participant_role, trace_id, created_at"
                " FROM usage_messages"
                " WHERE project_id = ? AND created_at >= ? AND created_at < ?"
                " ORDER BY created_at LIMIT ? OFFSET ?",
                (project_id, start_ts, end_ts, page_size, offset),
            ).fetchall()
        return [UsageMessageRow(**dict(r)) for r in rows]

    def purge_before(self, cutoff_iso: str, batch_size: int = 10_000) -> int:
        deleted = 0
        while True:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    "DELETE FROM usage_messages WHERE id IN"
                    " (SELECT id FROM usage_messages WHERE created_at < ? LIMIT ?)",
                    (cutoff_iso, batch_size),
                )
                n = cur.rowcount
            deleted += n
            if n < batch_size:
                break
        return deleted


class UsageHitlEventRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def record(self, row: UsageHitlEventRow) -> None:
        if row.event_type not in HITL_EVENT_TYPES:
            raise ValueError(f"Invalid event_type: {row.event_type!r}")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO usage_hitl_events"
                " (project_id, event_type, ticket_id, trace_id, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    row.project_id, row.event_type, row.ticket_id,
                    row.trace_id, row.created_at,
                ),
            )

    def count_for_day(self, *, project_id: int, day_utc: str) -> int:
        """Implemented in Story 14.05 (roll-up reads raw rows)."""
        raise NotImplementedError

    def list_for_day(
        self, *, project_id: int, day_utc: str, page: int = 1, page_size: int = 100
    ) -> list[UsageHitlEventRow]:
        start_ts = f"{day_utc}T00:00:00Z"
        end_ts = f"{(date.fromisoformat(day_utc) + timedelta(days=1)).isoformat()}T00:00:00Z"
        offset = (page - 1) * page_size
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, project_id, event_type, ticket_id, trace_id, created_at"
                " FROM usage_hitl_events"
                " WHERE project_id = ? AND created_at >= ? AND created_at < ?"
                " ORDER BY created_at LIMIT ? OFFSET ?",
                (project_id, start_ts, end_ts, page_size, offset),
            ).fetchall()
        return [UsageHitlEventRow(**dict(r)) for r in rows]

    def purge_before(self, cutoff_iso: str, batch_size: int = 10_000) -> int:
        deleted = 0
        while True:
            with sqlite3.connect(self._db_path) as conn:
                cur = conn.execute(
                    "DELETE FROM usage_hitl_events WHERE id IN"
                    " (SELECT id FROM usage_hitl_events WHERE created_at < ? LIMIT ?)",
                    (cutoff_iso, batch_size),
                )
                n = cur.rowcount
            deleted += n
            if n < batch_size:
                break
        return deleted


class UsageDailySummaryRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def upsert(self, row: UsageDailySummaryRow) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO usage_daily_summary
                    (project_id, day_utc, tracker_type, model_name,
                     prompt_tokens_total, completion_tokens_total, cost_usd_total,
                     wasted_cost_usd, call_count, in_count, out_count,
                     hitl_created_count, hitl_assigned_count,
                     hitl_replied_count, hitl_resolved_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_id, day_utc, tracker_type, model_name)
                DO UPDATE SET
                    prompt_tokens_total     = excluded.prompt_tokens_total,
                    completion_tokens_total = excluded.completion_tokens_total,
                    cost_usd_total          = excluded.cost_usd_total,
                    wasted_cost_usd         = excluded.wasted_cost_usd,
                    call_count              = excluded.call_count,
                    in_count                = excluded.in_count,
                    out_count               = excluded.out_count,
                    hitl_created_count      = excluded.hitl_created_count,
                    hitl_assigned_count     = excluded.hitl_assigned_count,
                    hitl_replied_count      = excluded.hitl_replied_count,
                    hitl_resolved_count     = excluded.hitl_resolved_count
                """,
                (
                    row.project_id, row.day_utc, row.tracker_type, row.model_name,
                    row.prompt_tokens_total, row.completion_tokens_total,
                    row.cost_usd_total, row.wasted_cost_usd, row.call_count,
                    row.in_count, row.out_count,
                    row.hitl_created_count, row.hitl_assigned_count,
                    row.hitl_replied_count, row.hitl_resolved_count,
                ),
            )

    def query(
        self,
        *,
        project_id: int,
        from_day_utc: str,
        to_day_utc: str,
        trackers: list[str] | None = None,
        include_money: bool = True,
    ) -> list[UsageDailySummaryRow]:
        params: list[object] = [project_id, from_day_utc, to_day_utc]
        tracker_clause = ""
        if trackers:
            placeholders = ",".join("?" * len(trackers))
            tracker_clause = f" AND tracker_type IN ({placeholders})"
            params.extend(trackers)
        money_cols = "cost_usd_total, wasted_cost_usd," if include_money else ""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT project_id, day_utc, tracker_type, model_name,
                       prompt_tokens_total, completion_tokens_total,
                       {money_cols}
                       call_count, in_count, out_count,
                       hitl_created_count, hitl_assigned_count,
                       hitl_replied_count, hitl_resolved_count
                FROM usage_daily_summary
                WHERE project_id = ? AND day_utc BETWEEN ? AND ?
                {tracker_clause}
                ORDER BY day_utc, tracker_type, model_name
                """,
                params,
            ).fetchall()
        return [
            UsageDailySummaryRow(
                project_id=r["project_id"],
                day_utc=r["day_utc"],
                tracker_type=r["tracker_type"],
                model_name=r["model_name"],
                prompt_tokens_total=r["prompt_tokens_total"],
                completion_tokens_total=r["completion_tokens_total"],
                cost_usd_total=r["cost_usd_total"] if include_money else None,
                wasted_cost_usd=r["wasted_cost_usd"] if include_money else None,
                call_count=r["call_count"],
                in_count=r["in_count"],
                out_count=r["out_count"],
                hitl_created_count=r["hitl_created_count"],
                hitl_assigned_count=r["hitl_assigned_count"],
                hitl_replied_count=r["hitl_replied_count"],
                hitl_resolved_count=r["hitl_resolved_count"],
            )
            for r in rows
        ]

    def query_wasted(
        self,
        *,
        project_id: int,
        from_day_utc: str,
        to_day_utc: str,
    ) -> list[UsageDailySummaryRow]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT project_id, day_utc, tracker_type, model_name,
                       prompt_tokens_total, completion_tokens_total,
                       cost_usd_total, wasted_cost_usd,
                       call_count, in_count, out_count,
                       hitl_created_count, hitl_assigned_count,
                       hitl_replied_count, hitl_resolved_count
                FROM usage_daily_summary
                WHERE project_id = ? AND tracker_type = 'llm'
                  AND day_utc BETWEEN ? AND ?
                ORDER BY day_utc, model_name
                """,
                (project_id, from_day_utc, to_day_utc),
            ).fetchall()
        return [
            UsageDailySummaryRow(
                project_id=r["project_id"],
                day_utc=r["day_utc"],
                tracker_type=r["tracker_type"],
                model_name=r["model_name"],
                prompt_tokens_total=r["prompt_tokens_total"],
                completion_tokens_total=r["completion_tokens_total"],
                cost_usd_total=r["cost_usd_total"],
                wasted_cost_usd=r["wasted_cost_usd"],
                call_count=r["call_count"],
                in_count=r["in_count"],
                out_count=r["out_count"],
                hitl_created_count=r["hitl_created_count"],
                hitl_assigned_count=r["hitl_assigned_count"],
                hitl_replied_count=r["hitl_replied_count"],
                hitl_resolved_count=r["hitl_resolved_count"],
            )
            for r in rows
        ]


class UsageIncidentRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def start(self, row: UsageIncidentRow) -> int:
        """Implemented in Story 14.09 (alerting / incident state machine)."""
        raise NotImplementedError

    def expand(self, *, incident_id: int, additional_trackers: str) -> None:
        """Implemented in Story 14.09 (INCIDENT_EXPAND transition)."""
        raise NotImplementedError

    def end(self, *, incident_id: int, ended_at: str) -> None:
        """Implemented in Story 14.09 (INCIDENT_END transition)."""
        raise NotImplementedError

    def active_for_project(self, *, project_id: int) -> UsageIncidentRow | None:
        """Implemented in Story 14.09 (check for open incident before START)."""
        raise NotImplementedError

    def list_for_window(
        self,
        *,
        project_id: int,
        from_ts: str,
        to_ts: str,
    ) -> list[UsageIncidentRow]:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, project_id, started_at, ended_at,
                       breached_trackers, peak_pct, total_excess_cost_usd
                FROM usage_incidents
                WHERE project_id = ? AND started_at >= ? AND started_at <= ?
                ORDER BY started_at
                """,
                (project_id, from_ts, to_ts),
            ).fetchall()
        return [UsageIncidentRow(**dict(r)) for r in rows]
