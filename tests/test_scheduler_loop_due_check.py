"""Tests for UsageRollupJob + UsageRetentionJob due-check and state persistence.

Story 14.05: scheduler wiring — both jobs implement the _Job protocol, check
whether they're due on each tick, and persist their run dates to a JSON file.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from services.api.app.usage.migrations import bootstrap_usage_db
from services.api.app.usage.repositories import (
    UsageDailySummaryRepository,
    UsageHitlEventRepository,
    UsageLlmCallRepository,
    UsageMessageRepository,
)
from services.scheduler.app.jobs.usage_retention_job import UsageRetentionJob
from services.scheduler.app.jobs.usage_rollup_job import UsageRollupJob
from services.scheduler.app.usage_rollup import RollupRepos


def _make_repos(tmp_path: Path) -> RollupRepos:
    db = str(tmp_path / "usage.db")
    bootstrap_usage_db(db)
    return RollupRepos(
        llm=UsageLlmCallRepository(db_path=db),
        messages=UsageMessageRepository(db_path=db),
        hitl=UsageHitlEventRepository(db_path=db),
        summary=UsageDailySummaryRepository(db_path=db),
        db_path=db,
    )


def _clock(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# UsageRollupJob — due check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rollup_job_runs_when_due(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    # Past the 00:05 UTC trigger with no prior run
    job = UsageRollupJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0,
    )

    with patch(
        "services.scheduler.app.jobs.usage_rollup_job.run_rollup",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_rollup_job_skips_before_rollup_time(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    # Before 00:05 UTC
    job = UsageRollupJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:01:00Z"),
        state_path=state_file, rollup_hour_utc=0,
    )

    with patch(
        "services.scheduler.app.jobs.usage_rollup_job.run_rollup",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_not_called()


@pytest.mark.asyncio
async def test_rollup_job_skips_when_already_ran_today(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    # State says: already ran on 2026-05-26
    state_file_path = Path(state_file)
    state_file_path.write_text(json.dumps({"last_rollup_date_utc": "2026-05-26"}))

    job = UsageRollupJob(
        repos=repos, clock=lambda: _clock("2026-05-26T01:00:00Z"),
        state_path=state_file, rollup_hour_utc=0,
    )

    with patch(
        "services.scheduler.app.jobs.usage_rollup_job.run_rollup",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_not_called()


@pytest.mark.asyncio
async def test_rollup_job_saves_state_after_run(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    job = UsageRollupJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0,
    )

    with patch(
        "services.scheduler.app.jobs.usage_rollup_job.run_rollup",
        new_callable=AsyncMock,
    ):
        await job.run()

    data = json.loads(Path(state_file).read_text())
    assert data["last_rollup_date_utc"] == "2026-05-26"


@pytest.mark.asyncio
async def test_rollup_job_runs_when_state_file_missing(tmp_path):
    repos = _make_repos(tmp_path)
    # No state file created
    state_file = str(tmp_path / "scheduler_runs.json")
    job = UsageRollupJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0,
    )

    with patch(
        "services.scheduler.app.jobs.usage_rollup_job.run_rollup",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_rollup_job_runs_when_state_file_malformed(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    Path(state_file).write_text("not valid json{{")
    job = UsageRollupJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0,
    )

    with patch(
        "services.scheduler.app.jobs.usage_rollup_job.run_rollup",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_called_once()


# ---------------------------------------------------------------------------
# UsageRetentionJob — due check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_job_runs_when_due(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    job = UsageRetentionJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0, retention_days=30,
    )

    with patch(
        "services.scheduler.app.jobs.usage_retention_job.run_retention",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_called_once()


@pytest.mark.asyncio
async def test_retention_job_skips_before_rollup_time(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    # Before 00:05 UTC — not yet due
    job = UsageRetentionJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:01:00Z"),
        state_path=state_file, rollup_hour_utc=0, retention_days=30,
    )

    with patch(
        "services.scheduler.app.jobs.usage_retention_job.run_retention",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_not_called()


@pytest.mark.asyncio
async def test_retention_job_skips_when_already_ran_today(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    Path(state_file).write_text(json.dumps({"last_retention_date_utc": "2026-05-26"}))
    job = UsageRetentionJob(
        repos=repos, clock=lambda: _clock("2026-05-26T01:00:00Z"),
        state_path=state_file, rollup_hour_utc=0, retention_days=30,
    )

    with patch(
        "services.scheduler.app.jobs.usage_retention_job.run_retention",
        new_callable=AsyncMock,
    ) as mock_fn:
        await job.run()

    mock_fn.assert_not_called()


@pytest.mark.asyncio
async def test_retention_job_saves_state_after_run(tmp_path):
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    job = UsageRetentionJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0, retention_days=30,
    )

    with patch(
        "services.scheduler.app.jobs.usage_retention_job.run_retention",
        new_callable=AsyncMock,
    ):
        await job.run()

    data = json.loads(Path(state_file).read_text())
    assert data["last_retention_date_utc"] == "2026-05-26"


@pytest.mark.asyncio
async def test_retention_job_preserves_rollup_key_in_state(tmp_path):
    """Writing retention state must not clobber the rollup key."""
    repos = _make_repos(tmp_path)
    state_file = str(tmp_path / "scheduler_runs.json")
    Path(state_file).write_text(
        json.dumps({"last_rollup_date_utc": "2026-05-25"})
    )
    job = UsageRetentionJob(
        repos=repos, clock=lambda: _clock("2026-05-26T00:10:00Z"),
        state_path=state_file, rollup_hour_utc=0, retention_days=30,
    )

    with patch(
        "services.scheduler.app.jobs.usage_retention_job.run_retention",
        new_callable=AsyncMock,
    ):
        await job.run()

    data = json.loads(Path(state_file).read_text())
    assert data["last_rollup_date_utc"] == "2026-05-25"
    assert data["last_retention_date_utc"] == "2026-05-26"


# ---------------------------------------------------------------------------
# Job protocol — has name attribute
# ---------------------------------------------------------------------------

def test_rollup_job_has_name(tmp_path):
    repos = _make_repos(tmp_path)
    job = UsageRollupJob(
        repos=repos,
        clock=lambda: datetime(2026, 5, 26, tzinfo=UTC),
        state_path=str(tmp_path / "s.json"),
    )
    assert isinstance(job.name, str)
    assert job.name


def test_retention_job_has_name(tmp_path):
    repos = _make_repos(tmp_path)
    job = UsageRetentionJob(
        repos=repos,
        clock=lambda: datetime(2026, 5, 26, tzinfo=UTC),
        state_path=str(tmp_path / "s.json"),
    )
    assert isinstance(job.name, str)
    assert job.name
