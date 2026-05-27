"""Two-turn round trip through the proposing stage (Story 12.07).

Turn A: bot proposes a date. Turn B: customer counter-offers a new
date; bot re-proposes from the new window using the updated intent.
The integration uses the real :class:`DateProposer` against an
in-memory ``availability_compute`` stub so the proposer ↔ answerer
seam is exercised end-to-end, but no calendar provider is touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.date_proposer import (
    AvailabilitySlot,
    DateProposer,
)
from services.api.app.sales.intent import Intent
from services.api.app.sales.sales_persona_answerer import (
    STAGE_PROPOSING,
    SalesPersonaAnswerer,
)


@dataclass
class _Service:
    id: int
    name: str


class _FakeStateRepo:
    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}

    def get(self, chat_id: int):
        return self.rows.get(chat_id)

    def upsert(self, **kwargs: Any) -> None:
        chat_id = int(kwargs["chat_id"])
        existing = self.rows.get(chat_id, {})
        merged = dict(existing)
        merged.update({k: v for k, v in kwargs.items() if v is not None})
        # last_proposal=None should still be respected when explicitly passed,
        # but the answerer never clears it on the proposing path.
        if "last_proposal" in kwargs:
            merged["last_proposal"] = kwargs["last_proposal"]
        self.rows[chat_id] = merged


class _FakeServicesRepoOuter:
    """The outer (answerer-side) services repo. The answerer itself never
    consults it for the proposing path — the inner DateProposer does."""

    def count_active(self, *, project_id: int) -> int:  # pragma: no cover
        return 1

    def list_for_project(self, *, project_id: int) -> list:  # pragma: no cover
        return [_Service(id=42, name="Каньонинг")]


class _InnerServicesRepo:
    def __init__(self, services: list[_Service]) -> None:
        self._services = services

    def list_for_project(self, *, project_id: int) -> list[_Service]:
        return list(self._services)


class _SettingsRepo:
    def is_enabled(self, project_id: int) -> bool:
        return True


class _SlotByDate:
    """Returns the slot for the FIRST date in the window's range that has one."""

    def __init__(self, slots: dict[date, AvailabilitySlot]) -> None:
        self._slots = slots
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        project_id: int,
        service_id: int,
        window: tuple[date, date],
        now: datetime,
    ):
        self.calls.append(
            {
                "project_id": project_id,
                "service_id": service_id,
                "window": window,
                "now": now,
            }
        )
        start, end = window
        cursor = start
        while cursor <= end:
            slot = self._slots.get(cursor)
            if slot is not None:
                return slot
            cursor = date.fromordinal(cursor.toordinal() + 1)
        return None


class _FakeOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        if not self.queue:
            raise AssertionError("LLM called without a queued payload")
        return self.queue.pop(0)


_NOW = datetime(2026, 4, 25, 9, 0, tzinfo=UTC)


def _ctx() -> AnswerContext:
    return AnswerContext(
        chat_id=7,
        customer_username="darya",
        trace_id="trace-rt",
        now=_NOW,
        project_id=1,
    )


@pytest.mark.asyncio
async def test_two_turn_round_trip_with_counter_offer() -> None:
    slots = {
        date(2026, 5, 1): AvailabilitySlot(
            date=date(2026, 5, 1),
            start_time=time(14, 0),
            end_time=time(15, 0),
        ),
        date(2026, 5, 2): AvailabilitySlot(
            date=date(2026, 5, 2),
            start_time=time(10, 0),
            end_time=time(11, 0),
        ),
    }
    availability = _SlotByDate(slots)
    proposer = DateProposer(
        availability_compute=availability,
        services_repo=_InnerServicesRepo([_Service(id=42, name="Каньонинг")]),
        settings_repo=_SettingsRepo(),
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
    )
    state_repo = _FakeStateRepo()
    openrouter = _FakeOpenRouter()
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_FakeServicesRepoOuter(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Николай",
        date_proposer=proposer,
    )

    # Seed proposing with intent.dates already pointing at 1 May.
    state_repo.rows[7] = {
        "chat_id": 7,
        "project_id": 1,
        "current_stage": STAGE_PROPOSING,
        "collected_intent": Intent(dates="1 мая").to_dict(),
        "last_proposal": None,
        "last_customer_msg_at": None,
        "last_bot_msg_at": None,
    }

    # Turn A — bot proposes May 1 at 14:00.
    openrouter.queue_response(
        {"text": "Предлагаю на 1 мая с началом в 14:00."}
    )
    result_a = await answerer.try_answer(question="ну что?", ctx=_ctx())
    assert result_a.handled is True
    assert "1 мая" in (result_a.text or "")
    assert "14:00" in (result_a.text or "")
    assert state_repo.rows[7]["last_proposal"]["date_iso"] == "2026-05-01"

    # Turn B — customer counter-offers 2 мая; bot re-proposes May 2 at 10:00.
    openrouter.queue_response(
        {"text": "Предлагаю на 2 мая с началом в 10:00."}
    )
    result_b = await answerer.try_answer(question="лучше 2 мая", ctx=_ctx())
    assert result_b.handled is True
    assert "2 мая" in (result_b.text or "")
    assert "10:00" in (result_b.text or "")

    # Proposer was called twice — once for each turn. The second call's
    # window is derived from the customer's counter-offer.
    assert len(availability.calls) == 2
    assert availability.calls[0]["window"] == (
        date(2026, 5, 1),
        date(2026, 5, 1),
    )
    assert availability.calls[1]["window"] == (
        date(2026, 5, 2),
        date(2026, 5, 2),
    )

    # State now holds the second proposal.
    assert state_repo.rows[7]["last_proposal"]["date_iso"] == "2026-05-02"
    assert state_repo.rows[7]["current_stage"] == STAGE_PROPOSING
