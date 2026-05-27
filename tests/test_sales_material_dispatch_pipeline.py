"""Integration test for Story 12.05 — full inbound → answerer → dispatch.

Drives the live ``SalesPersonaAnswerer`` with a real
``ClientMaterialsRepository`` + a real ``ClientMaterialsSelector``. The
dispatcher injected into the answerer wraps a stubbed
``TelegramBotSender`` so the test asserts (a) the answerer dispatched the
picked material, and (b) ``telegram_file_id`` was cached on the row after
the first send.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.client_materials_repository import (
    ClientMaterialsRepository,
)
from services.api.app.sales.client_materials_selector import (
    ClientMaterialsSelector,
)
from services.api.app.sales.sales_persona_answerer import (
    SalesPersonaAnswerer,
)
from services.api.app.sales.state_repository import StateRepository

_NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1


class _StubOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        return self.queue.pop(0)


class _NoOpServicesRepo:
    def count_active(self, *, project_id: int) -> int:
        return 0

    def list_for_project(self, *, project_id: int) -> list[Any]:
        return []

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:
        return None


class _StubSender:
    """Spy on send_video / send_photo / send_document. Returns ``ok=True``
    with a freshly assigned file_id so the api wrapper caches it."""

    def __init__(self, *, new_file_id: str) -> None:
        self._new_file_id = new_file_id
        self.disk_reads: list[Path] = []

    async def send_video(
        self,
        *,
        chat_id: int,
        file_id: str | None = None,
        local_path: Path | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        if file_id is None and local_path is not None:
            # Real upload path — read the bytes off disk.
            self.disk_reads.append(local_path)
            return {"ok": True, "telegram_file_id": self._new_file_id}
        return {"ok": True, "telegram_file_id": file_id}

    async def send_photo(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    async def send_document(self, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


def _ctx(*, trace_id: str = "trc-1") -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@danil",
        trace_id=trace_id,
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _make_dispatcher(
    *, sender: _StubSender, repo: ClientMaterialsRepository
):
    """Bind a callable that mimics the api `/sales/dispatch/material` semantics."""

    async def dispatch(
        *,
        chat_id: int,
        material_id: int,
        trace_id: str | None,
        caption_override: str | None = None,
    ) -> dict[str, Any]:
        row = repo.get(material_id=material_id)
        assert row is not None
        cached = row.telegram_file_id is not None
        result = await sender.send_video(
            chat_id=chat_id,
            file_id=row.telegram_file_id,
            local_path=None if cached else Path(row.local_path),
            caption=caption_override or row.caption,
        )
        if not cached and result.get("telegram_file_id"):
            repo.update_telegram_file_id(
                material_id=material_id,
                telegram_file_id=str(result["telegram_file_id"]),
            )
        return {"ok": True, "telegram_file_id_cached": cached}

    return dispatch


@pytest.mark.asyncio
async def test_scoping_completion_dispatches_and_caches_file_id(
    tmp_path: Path,
) -> None:
    sales_db = str(tmp_path / "sales.sqlite3")
    state_repo = StateRepository(db_path=sales_db)
    materials_repo = ClientMaterialsRepository(db_path=sales_db)
    selector = ClientMaterialsSelector(repo=materials_repo)
    sender = _StubSender(new_file_id="FRESH-TG-VID")

    video_path = tmp_path / "tour.mp4"
    video_path.write_bytes(b"mp4-bytes")
    material_id = materials_repo.add(
        project_id=_PROJECT_ID,
        kind="video",
        local_path=str(video_path),
        byte_size=9,
        caption="Тур-превью",
        tags=["tour_preview"],
        now=_NOW,
    )

    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято! Готовлю программу.",
        }
    )

    # Seed the state row at 4/5 fields so the next turn completes scoping.
    state_repo.upsert(
        chat_id=_CHAT_ID,
        project_id=_PROJECT_ID,
        current_stage="scoping",
        collected_intent={
            "dates": "1 мая",
            "headcount": 4,
            "vehicle_count": 2,
            "difficulty": "начальный",
            "drivers": None,
        },
        now=_NOW,
        last_bot_msg_at=_NOW,
    )

    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        material_selector=selector,
        material_dispatcher=_make_dispatcher(
            sender=sender, repo=materials_repo
        ),
    )

    result = await answerer.try_answer(question="1 водитель", ctx=_ctx())
    assert result.handled is True
    assert "Готовлю программу" in (result.text or "")
    # Fresh upload: disk was read once.
    assert sender.disk_reads == [video_path]
    # Cached on the row.
    row = materials_repo.get(material_id=material_id)
    assert row is not None
    assert row.telegram_file_id == "FRESH-TG-VID"

    # Second customer (different chat) reaches the same moment → uses cache.
    second_chat_id = 8
    state_repo.upsert(
        chat_id=second_chat_id,
        project_id=_PROJECT_ID,
        current_stage="scoping",
        collected_intent={
            "dates": "1 мая",
            "headcount": 4,
            "vehicle_count": 2,
            "difficulty": "начальный",
            "drivers": None,
        },
        now=_NOW,
        last_bot_msg_at=_NOW,
    )
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Готово!",
        }
    )
    ctx2 = AnswerContext(
        chat_id=second_chat_id,
        customer_username="@второй",
        trace_id="trc-2",
        now=_NOW,
        project_id=_PROJECT_ID,
    )
    await answerer.try_answer(question="1 водитель", ctx=ctx2)
    # No additional disk read — the cached file_id was used.
    assert sender.disk_reads == [video_path]
