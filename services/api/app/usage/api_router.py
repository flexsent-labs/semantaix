"""Usage API endpoints + money RBAC (Story 14.07).

Four read-only endpoints on the ``api`` service (NOT web_ui):

- GET /api/usage/summary  — daily summary rows (admin: money included;
                             operator: money columns excluded at SQL level)
- GET /api/usage/raw      — paginated raw rows for a single day_utc
                             (410 if day is outside 30-day retention window;
                              operator: cost_usd excluded at SQL level for llm)
- GET /api/usage/wasted   — LLM summary rows with wasted cost (admin only)
- GET /api/usage/incidents — incidents in a time window (both roles)

Auth: cookie session OR ``Authorization: Bearer <internal_service_token>``
with ``as_user=<username>`` query parameter (bot path).

Operator project scoping: operators may only query their own project_id.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query, Request

from platform_common.settings import get_settings
from services.api.app.admin_auth import AdminAuthService
from services.api.app.operators import OperatorRepository
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageIncidentRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)

_MAX_RETENTION_DAYS = get_settings().usage_raw_retention_days
_MAX_PAGE_SIZE = 500


def _check_retention_window(day_utc: str) -> None:
    """Raise 410 if day_utc is outside the 30-day retention window."""
    try:
        day = datetime.fromisoformat(day_utc).date()
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_day_utc")
    cutoff = (datetime.now(UTC) - timedelta(days=_MAX_RETENTION_DAYS)).date()
    if day < cutoff:
        raise HTTPException(status_code=410, detail="data_purged")


def _enforce_operator_project(
    principal_role: str,
    principal_username: str,
    project_id: int,
    operator_repo: OperatorRepository,
) -> None:
    if principal_role != "operator":
        return
    op = operator_repo.find_by_username(principal_username)
    if op is None or op.project_id != project_id:
        raise HTTPException(status_code=403, detail="project_not_allowed")


def _parse_trackers(trackers_param: str | None) -> list[str] | None:
    if not trackers_param:
        return None
    parts = [t.strip() for t in trackers_param.split(",") if t.strip()]
    return parts if parts else None


def wire_usage_api_routes(
    app: FastAPI,
    *,
    auth_service: AdminAuthService,
    summary_repo: UsageDailySummaryRepository,
    llm_repo: UsageLlmCallRepository,
    message_repo: UsageMessageRepository,
    hitl_repo: UsageHitlEventRepository,
    incident_repo: UsageIncidentRepository,
    operator_repo: OperatorRepository,
) -> None:
    @app.get("/api/usage/summary")
    def _summary(
        request: Request,
        project_id: int,
        from_day_utc: str,
        to_day_utc: str,
        trackers: str | None = None,
        as_user: str | None = None,
    ) -> dict:
        principal = auth_service.require_session_or_internal(request, as_user)
        _enforce_operator_project(
            principal.role, principal.username, project_id, operator_repo
        )
        tracker_list = _parse_trackers(trackers)
        include_money = principal.role == "admin"
        rows = summary_repo.query(
            project_id=project_id,
            from_day_utc=from_day_utc,
            to_day_utc=to_day_utc,
            trackers=tracker_list,
            include_money=include_money,
        )
        return {"rows": [dataclasses.asdict(r) for r in rows]}

    @app.get("/api/usage/raw")
    def _raw(
        request: Request,
        project_id: int,
        day_utc: str,
        tracker_type: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=_MAX_PAGE_SIZE),
        as_user: str | None = None,
    ) -> dict:
        principal = auth_service.require_session_or_internal(request, as_user)
        _enforce_operator_project(
            principal.role, principal.username, project_id, operator_repo
        )
        if tracker_type not in ("llm", "messages", "hitl"):
            raise HTTPException(status_code=400, detail="invalid_tracker_type")
        _check_retention_window(day_utc)
        include_money = principal.role == "admin"
        if tracker_type == "llm":
            raw_rows = llm_repo.list_for_day(
                project_id=project_id,
                day_utc=day_utc,
                page=page,
                page_size=page_size,
                include_money=include_money,
            )
        elif tracker_type == "messages":
            raw_rows = message_repo.list_for_day(
                project_id=project_id,
                day_utc=day_utc,
                page=page,
                page_size=page_size,
            )
        else:
            raw_rows = hitl_repo.list_for_day(
                project_id=project_id,
                day_utc=day_utc,
                page=page,
                page_size=page_size,
            )
        return {
            "rows": [dataclasses.asdict(r) for r in raw_rows],
            "has_more": len(raw_rows) == page_size,
        }

    @app.get("/api/usage/wasted")
    def _wasted(
        request: Request,
        project_id: int,
        from_day_utc: str,
        to_day_utc: str,
        as_user: str | None = None,
    ) -> dict:
        principal = auth_service.require_session_or_internal(request, as_user)
        if principal.role != "admin":
            raise HTTPException(status_code=403, detail="admin_only")
        rows = summary_repo.query_wasted(
            project_id=project_id,
            from_day_utc=from_day_utc,
            to_day_utc=to_day_utc,
        )
        return {"rows": [dataclasses.asdict(r) for r in rows]}

    @app.get("/api/usage/incidents")
    def _incidents(
        request: Request,
        project_id: int,
        from_ts: str,
        to_ts: str,
        as_user: str | None = None,
    ) -> dict:
        principal = auth_service.require_session_or_internal(request, as_user)
        _enforce_operator_project(
            principal.role, principal.username, project_id, operator_repo
        )
        incidents = incident_repo.list_for_window(
            project_id=project_id,
            from_ts=from_ts,
            to_ts=to_ts,
        )
        return {"incidents": [dataclasses.asdict(i) for i in incidents]}
