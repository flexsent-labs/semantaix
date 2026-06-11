"""Usage repository skeletons and row types (Story 14.01).

Each repository owns one ``semantaix_usage.db`` table.  Methods raise
``NotImplementedError`` until the indicated story implements them.

Sync ``sqlite3`` per project convention; callers dispatch via
``asyncio.to_thread``.
"""

from __future__ import annotations

from dataclasses import dataclass


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
        """Implemented in Story 14.02."""
        raise NotImplementedError

    def list_for_day(self, *, project_id: int, day_utc: str) -> list[UsageLlmCallRow]:
        """Implemented in Story 14.05 (roll-up reads raw rows)."""
        raise NotImplementedError


class UsageMessageRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def record(self, row: UsageMessageRow) -> None:
        """Implemented in Story 14.03."""
        raise NotImplementedError

    def count_for_day(self, *, project_id: int, day_utc: str) -> int:
        """Implemented in Story 14.05 (roll-up reads raw rows)."""
        raise NotImplementedError


class UsageHitlEventRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def record(self, row: UsageHitlEventRow) -> None:
        """Implemented in Story 14.04."""
        raise NotImplementedError

    def count_for_day(self, *, project_id: int, day_utc: str) -> int:
        """Implemented in Story 14.05 (roll-up reads raw rows)."""
        raise NotImplementedError


class UsageDailySummaryRepository:
    def __init__(self, *, db_path: str) -> None:
        self._db_path = db_path

    def upsert(self, row: UsageDailySummaryRow) -> None:
        """Implemented in Story 14.05 (daily roll-up worker)."""
        raise NotImplementedError

    def query(
        self,
        *,
        project_id: int,
        from_day: str,
        to_day: str,
    ) -> list[UsageDailySummaryRow]:
        """Implemented in Story 14.07 (usage API)."""
        raise NotImplementedError

    def query_wasted(
        self,
        *,
        project_id: int,
        from_day: str,
        to_day: str,
    ) -> list[UsageDailySummaryRow]:
        """Implemented in Story 14.07 (wasted-spend tile, admin-only)."""
        raise NotImplementedError


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
        """Implemented in Story 14.07 (usage incidents API endpoint)."""
        raise NotImplementedError
