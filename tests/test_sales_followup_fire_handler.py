"""Unit tests for ``FollowupFireHandler`` (Story 12.08)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.api.app.sales.followup_fire_handler import FollowupFireHandler
from services.api.app.sales.followup_queue_repository import (
    REASON_TELEGRAM_SEND_FAILED,
    STATUS_SCHEDULED,
    STATUS_SENT,
    STATUS_SKIPPED_STALE,
    FollowupQueueRepository,
)
from services.api.app.sales.state_repository import StateRepository

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=UTC)


def _build(tmp_path: Path, *, llm_payload: Any, send_side_effect: Any = None):
    state_repo = StateRepository(db_path=str(tmp_path / "state.db"))
    followup_repo = FollowupQueueRepository(db_path=str(tmp_path / "fu.db"))
    state_repo.upsert(
        chat_id=42,
        project_id=1,
        current_stage="scoping",
        collected_intent={"dates": "1 июня", "headcount": 4},
        now=_NOW,
    )
    row_id = followup_repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    send_mock = AsyncMock(
        return_value=99 if send_side_effect is None else None,
        side_effect=send_side_effect,
    )
    llm_mock = AsyncMock(return_value=llm_payload)
    handler = FollowupFireHandler(
        followup_repo=followup_repo,
        state_repo=state_repo,
        openrouter=type("_R", (), {"complete_json": llm_mock})(),
        telegram_sender=type("_S", (), {"send_message": send_mock})(),
        persona_getter=lambda: "Николай",
        clock=lambda: _NOW,
    )
    return {
        "handler": handler,
        "followup_repo": followup_repo,
        "row_id": row_id,
        "send_mock": send_mock,
        "llm_mock": llm_mock,
    }


@pytest.mark.asyncio
async def test_happy_path_sends_llm_text(tmp_path: Path) -> None:
    env = _build(tmp_path, llm_payload={"text": "Анна, остались вопросы?"})
    handler: FollowupFireHandler = env["handler"]
    row = env["followup_repo"].get(env["row_id"])
    assert row is not None
    outcome = await handler.fire(row, customer_name="Анна")
    assert outcome.sent is True
    assert outcome.fallback_text_used is False
    assert outcome.text == "Анна, остались вопросы?"
    env["send_mock"].assert_awaited_once_with(
        chat_id=42, text="Анна, остались вопросы?"
    )
    after = env["followup_repo"].get(env["row_id"])
    assert after is not None
    assert after.status == STATUS_SENT


@pytest.mark.asyncio
async def test_empty_llm_payload_falls_back(tmp_path: Path) -> None:
    env = _build(tmp_path, llm_payload={})
    handler: FollowupFireHandler = env["handler"]
    row = env["followup_repo"].get(env["row_id"])
    outcome = await handler.fire(row, customer_name="Анна")  # type: ignore[arg-type]
    assert outcome.sent is True
    assert outcome.fallback_text_used is True
    assert "Анна" in outcome.text  # type: ignore[operator]


@pytest.mark.asyncio
async def test_llm_exception_falls_back(tmp_path: Path) -> None:
    env = _build(tmp_path, llm_payload={"text": "ok"})
    env["llm_mock"].side_effect = RuntimeError("boom")
    env["llm_mock"].return_value = None
    handler: FollowupFireHandler = env["handler"]
    row = env["followup_repo"].get(env["row_id"])
    outcome = await handler.fire(row, customer_name="")  # type: ignore[arg-type]
    assert outcome.sent is True
    assert outcome.fallback_text_used is True
    assert outcome.text == "Остались вопросы по туру?"


@pytest.mark.asyncio
async def test_telegram_failure_marks_skipped_stale(tmp_path: Path) -> None:
    env = _build(
        tmp_path,
        llm_payload={"text": "ok"},
        send_side_effect=RuntimeError("network"),
    )
    handler: FollowupFireHandler = env["handler"]
    row = env["followup_repo"].get(env["row_id"])
    outcome = await handler.fire(row, customer_name="Анна")  # type: ignore[arg-type]
    assert outcome.sent is False
    after = env["followup_repo"].get(env["row_id"])
    assert after is not None
    assert after.status == STATUS_SKIPPED_STALE
    assert after.reason == REASON_TELEGRAM_SEND_FAILED


@pytest.mark.asyncio
async def test_falls_back_to_state_customer_name(tmp_path: Path) -> None:
    state_repo = StateRepository(db_path=str(tmp_path / "state.db"))
    followup_repo = FollowupQueueRepository(db_path=str(tmp_path / "fu.db"))
    row_id = followup_repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    state_repo.upsert(
        chat_id=42,
        project_id=1,
        current_stage="scoping",
        collected_intent={},
        now=_NOW,
    )
    handler = FollowupFireHandler(
        followup_repo=followup_repo,
        state_repo=state_repo,
        openrouter=type("_R", (), {
            "complete_json": AsyncMock(return_value={})
        })(),
        telegram_sender=type("_S", (), {
            "send_message": AsyncMock(return_value=1)
        })(),
        persona_getter=lambda: "Николай",
        clock=lambda: _NOW,
    )
    row = followup_repo.get(row_id)
    outcome = await handler.fire(row)  # type: ignore[arg-type]
    # No customer name available — fallback uses the no-name form.
    assert outcome.sent is True
    assert "вопросы" in outcome.text  # type: ignore[operator]


@pytest.mark.asyncio
async def test_state_update_failure_does_not_break_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _build(tmp_path, llm_payload={"text": "ok"})
    handler: FollowupFireHandler = env["handler"]

    class _BrokenState:
        def get(self, chat_id: int) -> dict[str, Any] | None:
            return {"current_stage": "scoping", "collected_intent": {}}

        def upsert(self, **kwargs: Any) -> None:
            raise RuntimeError("disk full")

    handler._state_repo = _BrokenState()  # type: ignore[attr-defined]
    row = env["followup_repo"].get(env["row_id"])
    outcome = await handler.fire(row)  # type: ignore[arg-type]
    assert outcome.sent is True
    after = env["followup_repo"].get(env["row_id"])
    assert after is not None
    assert after.status == STATUS_SENT


@pytest.mark.asyncio
async def test_state_get_returning_none_uses_empty_intent(
    tmp_path: Path,
) -> None:
    followup_repo = FollowupQueueRepository(db_path=str(tmp_path / "fu.db"))
    row_id = followup_repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )

    class _EmptyState:
        def get(self, chat_id: int) -> None:
            return None

        def upsert(self, **kwargs: Any) -> None:
            pass

    handler = FollowupFireHandler(
        followup_repo=followup_repo,
        state_repo=_EmptyState(),
        openrouter=type("_R", (), {
            "complete_json": AsyncMock(return_value={"text": "Hi"})
        })(),
        telegram_sender=type("_S", (), {
            "send_message": AsyncMock(return_value=1)
        })(),
        persona_getter=lambda: "Николай",
        clock=lambda: _NOW,
    )
    row = followup_repo.get(row_id)
    outcome = await handler.fire(row, customer_name="Аня")  # type: ignore[arg-type]
    assert outcome.sent is True


@pytest.mark.asyncio
async def test_extract_text_accepts_bare_string_payload(tmp_path: Path) -> None:
    env = _build(tmp_path, llm_payload="Голый текст без ключей")
    handler: FollowupFireHandler = env["handler"]
    row = env["followup_repo"].get(env["row_id"])
    outcome = await handler.fire(row)  # type: ignore[arg-type]
    assert outcome.sent is True
    assert outcome.fallback_text_used is False
    assert outcome.text == "Голый текст без ключей"


@pytest.mark.asyncio
async def test_uses_customer_first_name_from_state(tmp_path: Path) -> None:
    """``customer_first_name`` on the state row wins over the fallback arg."""

    class _NameState:
        def __init__(self) -> None:
            self.upserts: list[Any] = []

        def get(self, chat_id: int) -> dict[str, Any]:
            return {
                "current_stage": "scoping",
                "collected_intent": {},
                "customer_first_name": "Данил",
            }

        def upsert(self, **kwargs: Any) -> None:
            self.upserts.append(kwargs)

    followup_repo = FollowupQueueRepository(db_path=str(tmp_path / "fu.db"))
    row_id = followup_repo.enqueue(
        chat_id=42, project_id=1, fire_at=_NOW, now=_NOW
    )
    captured: dict[str, str] = {}

    async def _send_capture(*, chat_id: int, text: str) -> int:
        captured["chat_id"] = str(chat_id)
        captured["text"] = text
        return 1

    sender = type("_S", (), {})()
    sender.send_message = _send_capture  # type: ignore[attr-defined]

    handler = FollowupFireHandler(
        followup_repo=followup_repo,
        state_repo=_NameState(),
        openrouter=type("_R", (), {
            "complete_json": AsyncMock(return_value={})  # forces fallback
        })(),
        telegram_sender=sender,
        persona_getter=lambda: "Николай",
        clock=lambda: _NOW,
    )
    row = followup_repo.get(row_id)
    outcome = await handler.fire(row, customer_name="OverrideMe")  # type: ignore[arg-type]
    assert outcome.sent is True
    # State-level name beats the explicit override; fallback uses "Данил".
    assert captured["text"] == "Данил, остались вопросы по туру?"


def test_row_status_remains_until_fire(tmp_path: Path) -> None:
    repo = FollowupQueueRepository(db_path=str(tmp_path / "fu.db"))
    row_id = repo.enqueue(chat_id=42, project_id=1, fire_at=_NOW, now=_NOW)
    row = repo.get(row_id)
    assert row is not None
    assert row.status == STATUS_SCHEDULED
