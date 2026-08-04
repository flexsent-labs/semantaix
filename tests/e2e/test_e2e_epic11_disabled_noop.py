"""Epic 11 / story 11.07 — disabled-project no-op regression.

Proves that when calendar is OFF for a project (the default), an
availability-shaped question behaves exactly as it did before the calendar
answerer existed: it falls through to grounded RAG / HITL with no calendar work
(no settings.get, no token, no freeBusy) and no added latency.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.answerers.scope_guard import ScopeGuardAnswerer
from services.api.app.calendar.settings_repository import CalendarSettingsRepository
from services.api.app.main import app as api_app
from services.api.app.main import (
    hitl_ticket_repository,
    openrouter_client,
    rag_repository,
)
from services.api.app.openrouter_client import GroundingVerdict, LlmUsageCapture

pytestmark = [pytest.mark.e2e, pytest.mark.epic("11"), pytest.mark.story("11-07")]

_CHAT_ID = 9100


class _CountingSettings(CalendarSettingsRepository):
    """A real settings repo that records calls, to assert the cheap gate only."""

    def __init__(self, *, db_path: str) -> None:
        super().__init__(db_path=db_path)
        self.is_enabled_calls = 0
        self.get_calls = 0

    def is_enabled(self, project_id: int) -> bool:
        self.is_enabled_calls += 1
        return super().is_enabled(project_id)

    def get(self, project_id: int):
        self.get_calls += 1
        return super().get(project_id)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    # Empty calendar DB -> every project is disabled (default-off).
    settings_repo = _CountingSettings(db_path=str(tmp_path / "calendar.sqlite3"))

    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    api_main.incident_repository.db_path = str(tmp_path / "incidents.sqlite3")
    rag_repository.db_path = str(tmp_path / "rag.sqlite3")
    api_main.answer_trace_repository.db_path = str(tmp_path / "traces.sqlite3")
    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", AsyncMock(return_value=1)
    )
    monkeypatch.setattr(api_main, "calendar_settings_repository", settings_repo)
    # Rebuild the pipeline so its calendar answerer points at the disabled repo,
    # but with token/freebusy collaborators that MUST never be touched.
    token_provider = AsyncMock()
    freebusy_client = AsyncMock()
    from services.api.app.answerers import AnswerPipeline
    from services.api.app.calendar.availability_answerer import (
        CalendarAvailabilityAnswerer,
    )
    from services.api.app.calendar.clarify_state_repository import (
        CalendarClarifyStateRepository,
    )
    from services.api.app.russian_text import get_russian_normalizer

    calendar = CalendarAvailabilityAnswerer(
        settings_repo=settings_repo,
        token_provider=token_provider,
        freebusy_client=freebusy_client,
        normalizer=get_russian_normalizer(),
        clarify_store=CalendarClarifyStateRepository(
            db_path=str(tmp_path / "clarify.sqlite3")
        ),
        operator_chat_resolver=lambda operator: 1,
    )
    monkeypatch.setattr(
        api_main,
        "answer_pipeline",
        AnswerPipeline([
            calendar,
            next(a for a in api_main.answer_pipeline.answerers if a.name == "grounded_rag"),
            ScopeGuardAnswerer(
                phrases_getter=api_main._effective_scope_decline_messages,
                project_does_bookings=settings_repo.is_enabled,
            ),
        ]),
    )
    client = TestClient(api_app)
    yield {
        "client": client,
        "settings_repo": settings_repo,
        "token_provider": token_provider,
        "freebusy_client": freebusy_client,
    }


def test_e2e_disabled_project_availability_question_grounds_via_rag(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = env["client"]
    hitl_ticket_repository.set_runtime_config(
        key="rag_grounding_score_threshold", value="0.2", updated_by="@admin"
    )
    rag_repository.ingest(
        source_id="kb-manicure",
        text="Запись на маникюр доступна по субботам с 10 до 19",
    )
    monkeypatch.setattr(
        openrouter_client,
        "answer_grounded",
        AsyncMock(return_value=(
            "Маникюр доступен по субботам с 10 до 19.",
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=50, completion_tokens=25,
                cost_usd=0.001, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )
    monkeypatch.setattr(
        openrouter_client,
        "verify_grounding",
        AsyncMock(return_value=(
            GroundingVerdict(label="GROUNDED", reason="ok"),
            LlmUsageCapture(
                model_name="gpt-4o", prompt_tokens=20, completion_tokens=10,
                cost_usd=0.0005, created_at="2026-06-11T00:00:00Z",
            ),
        )),
    )

    body = client.post(
        "/conversations/inbound",
        json={
            "text": "можно записаться на маникюр в субботу в 15:00?",
            "chat_id": _CHAT_ID,
            "trace_id": "t-disabled-grounded",
        },
    ).json()

    # Behaves like any grounded question — RAG answered, calendar untouched.
    assert body["response_mode"] == "grounded_rag"
    # Calendar work limited to the single cheap gate; NO settings.get, token, or
    # freeBusy call — proving zero added behavior/latency when off.
    assert env["settings_repo"].is_enabled_calls == 1
    assert env["settings_repo"].get_calls == 0
    env["token_provider"].get_access_token.assert_not_called()
    env["freebusy_client"].query_busy.assert_not_called()


def test_e2e_disabled_project_availability_question_declines_when_no_rag(env) -> None:
    client = env["client"]
    body = client.post(
        "/conversations/inbound",
        json={
            "text": "можно записаться на маникюр в субботу в 15:00?",
            "chat_id": _CHAT_ID,
            "trace_id": "t-disabled-scope",
        },
    ).json()

    # No corpus + calendar disabled -> scope guard polite decline (no HITL ticket).
    from services.api.app.answerers.scope_guard import RESPONSE_MODE_SCOPE_DECLINE
    assert body["delivered"] is True
    assert body["escalated"] is False
    assert body["response_mode"] == RESPONSE_MODE_SCOPE_DECLINE
    env["token_provider"].get_access_token.assert_not_called()
    env["freebusy_client"].query_busy.assert_not_called()
