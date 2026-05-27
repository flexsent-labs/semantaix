"""Tests for the Story 12.05 ``SalesPersonaAnswerer`` media moments.

Two trigger points:

* When scoping completes (intent reaches 5/5 fields), the answerer hits
  the injected ``ClientMaterialsSelector`` with ``purpose="tour_preview"``
  and dispatches the picked material via the api ``/sales/dispatch/material``
  client. The textual pitch is still returned as the ``AnswerResult.text``
  so the customer is never left silent.
* When the customer asks about equipment (``"что нужно?"`` /
  ``"какое снаряжение?"`` / etc.) the answerer hits the selector with
  ``purpose="equipment_gallery"`` and dispatches the match.

In both branches:

* ``selector.pick(...) → None`` → no dispatch call, plain textual reply.
* ``dispatcher(...) → {ok: False}`` → textual fallback is appended within
  the same ``AnswerResult.text`` (so the customer always sees a sentence
  even when Telegram failed mid-dispatch).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.answerers import AnswerContext
from services.api.app.sales.client_materials_repository import ClientMaterial
from services.api.app.sales.sales_persona_answerer import (
    STAGE_PITCHING,
    STAGE_SCOPING,
    SalesPersonaAnswerer,
)

_NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
_CHAT_ID = 7
_PROJECT_ID = 1


def _material(
    *,
    material_id: int = 11,
    kind: str = "video",
    caption: str | None = "Гора Ачишхо",
    tags: list[str] | None = None,
) -> ClientMaterial:
    return ClientMaterial(
        id=material_id,
        project_id=_PROJECT_ID,
        kind=kind,
        telegram_file_id=None,
        local_path=f"/x/{material_id}.mp4",
        byte_size=10,
        duration_seconds=None,
        caption=caption,
        tags=tags or ["tour_preview"],
        source_operator_file_id=None,
        is_active=True,
        created_at=_NOW.isoformat(),
        updated_at=_NOW.isoformat(),
    )


@dataclass
class _SpySelector:
    picks: dict[str, ClientMaterial | None]
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def pick(
        self, *, project_id: int, intent_tags: list[str], purpose: str
    ) -> ClientMaterial | None:
        self.calls.append(
            {
                "project_id": project_id,
                "intent_tags": list(intent_tags),
                "purpose": purpose,
            }
        )
        return self.picks.get(purpose)


@dataclass
class _SpyDispatcher:
    outcomes: list[dict[str, Any]]
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    async def __call__(
        self,
        *,
        chat_id: int,
        material_id: int,
        trace_id: str | None,
        caption_override: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "chat_id": chat_id,
                "material_id": material_id,
                "trace_id": trace_id,
                "caption_override": caption_override,
            }
        )
        if not self.outcomes:
            return {"ok": True}
        return self.outcomes.pop(0)


class _StubOpenRouter:
    def __init__(self) -> None:
        self.queue: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    def queue_response(self, payload: dict[str, Any]) -> None:
        self.queue.append(payload)

    async def complete_json(
        self, *, system: str, user: str, model: str | None = None
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user})
        return self.queue.pop(0)


class _FakeStateRepo:
    def __init__(self, *, initial: dict[str, Any] | None = None) -> None:
        self._state: dict[str, Any] | None = initial
        self.upserts: list[dict[str, Any]] = []

    def get(self, chat_id: int) -> dict[str, Any] | None:
        return self._state

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)
        intent = kwargs.get("collected_intent") or {}
        self._state = {
            "chat_id": kwargs.get("chat_id"),
            "project_id": kwargs.get("project_id"),
            "current_stage": kwargs.get("current_stage"),
            "collected_intent": intent,
            "last_proposal": kwargs.get("last_proposal"),
        }


class _NoOpServicesRepo:
    def count_active(self, *, project_id: int) -> int:
        return 0

    def list_for_project(self, *, project_id: int) -> list[Any]:
        return []

    def get_by_name(self, *, project_id: int, name: str) -> Any | None:
        return None


def _ctx(*, trace_id: str = "trace-1") -> AnswerContext:
    return AnswerContext(
        chat_id=_CHAT_ID,
        customer_username="@danil",
        trace_id=trace_id,
        now=_NOW,
        project_id=_PROJECT_ID,
    )


def _scoping_state_with_4_fields() -> dict[str, Any]:
    return {
        "chat_id": _CHAT_ID,
        "project_id": _PROJECT_ID,
        "current_stage": STAGE_SCOPING,
        "collected_intent": {
            "dates": "1 мая",
            "headcount": 4,
            "vehicle_count": 2,
            "difficulty": "начальный",
            "drivers": None,
        },
    }


def _make_answerer(
    *,
    selector: _SpySelector | None,
    dispatcher: _SpyDispatcher | None,
    openrouter: _StubOpenRouter,
    state_repo: _FakeStateRepo,
) -> SalesPersonaAnswerer:
    from services.api.app.russian_text import get_russian_normalizer

    return SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        material_selector=selector,
        material_dispatcher=dispatcher,
    )


# --- scoping → pitching media moment ---------------------------------------


@pytest.mark.asyncio
async def test_scoping_completion_dispatches_tour_preview_material() -> None:
    selector = _SpySelector(picks={"tour_preview": _material()})
    dispatcher = _SpyDispatcher(outcomes=[{"ok": True}])
    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято! Готовлю программу.",
        }
    )
    state_repo = _FakeStateRepo(initial=_scoping_state_with_4_fields())
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=openrouter,
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="1 водитель", ctx=_ctx(trace_id="trc-A")
    )
    assert result.handled is True
    # Selector was consulted with purpose=tour_preview.
    assert len(selector.calls) == 1
    assert selector.calls[0]["purpose"] == "tour_preview"
    assert selector.calls[0]["project_id"] == _PROJECT_ID
    # Dispatcher was awaited with the right args + trace_id.
    assert dispatcher.calls == [
        {
            "chat_id": _CHAT_ID,
            "material_id": 11,
            "trace_id": "trc-A",
            "caption_override": None,
        }
    ]
    # Textual pitch is returned as usual.
    assert "Готовлю программу" in (result.text or "")


@pytest.mark.asyncio
async def test_scoping_completion_with_no_material_skips_dispatch() -> None:
    selector = _SpySelector(picks={"tour_preview": None})
    dispatcher = _SpyDispatcher(outcomes=[])
    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято! Готовлю программу.",
        }
    )
    state_repo = _FakeStateRepo(initial=_scoping_state_with_4_fields())
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=openrouter,
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="1 водитель", ctx=_ctx()
    )
    assert result.handled is True
    assert dispatcher.calls == []
    # No fabricated "вот видео" — the textual pitch stands alone.
    assert "видео" not in (result.text or "")


@pytest.mark.asyncio
async def test_scoping_completion_dispatch_failure_appends_fallback() -> None:
    selector = _SpySelector(picks={"tour_preview": _material()})
    dispatcher = _SpyDispatcher(
        outcomes=[{"ok": False, "error_reason": "telegram_send_failed"}]
    )
    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято.",
        }
    )
    state_repo = _FakeStateRepo(initial=_scoping_state_with_4_fields())
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=openrouter,
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="1 водитель", ctx=_ctx()
    )
    assert result.handled is True
    # Textual fallback line appended so the customer is not left silent.
    text = result.text or ""
    assert "Принято" in text
    assert "фото" in text.lower() or "позже" in text.lower() or "коллег" in text.lower()


@pytest.mark.asyncio
async def test_scoping_still_open_does_not_dispatch() -> None:
    """One field still missing → no media dispatch yet."""
    selector = _SpySelector(picks={"tour_preview": _material()})
    dispatcher = _SpyDispatcher(outcomes=[])
    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"headcount": 4},
            "next_question": "Сколько квадроциклов?",
        }
    )
    state_repo = _FakeStateRepo(
        initial={
            "chat_id": _CHAT_ID,
            "project_id": _PROJECT_ID,
            "current_stage": STAGE_SCOPING,
            "collected_intent": {},
        }
    )
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=openrouter,
        state_repo=state_repo,
    )
    result = await answerer.try_answer(question="нас 4", ctx=_ctx())
    assert result.handled is True
    assert selector.calls == []
    assert dispatcher.calls == []


# --- equipment Q&A media moment ---------------------------------------------


@pytest.mark.asyncio
async def test_equipment_ask_dispatches_equipment_gallery_material() -> None:
    selector = _SpySelector(
        picks={"equipment_gallery": _material(material_id=22, kind="photo")}
    )
    dispatcher = _SpyDispatcher(outcomes=[{"ok": True}])
    openrouter = _StubOpenRouter()
    state_repo = _FakeStateRepo(
        initial={
            "chat_id": _CHAT_ID,
            "project_id": _PROJECT_ID,
            "current_stage": STAGE_PITCHING,
            "collected_intent": {
                "dates": "1 мая",
                "headcount": 4,
                "vehicle_count": 2,
                "difficulty": "начальный",
                "drivers": 1,
            },
        }
    )
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=openrouter,
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="а какое снаряжение нужно?", ctx=_ctx(trace_id="trc-eq")
    )
    assert result.handled is True
    assert any(
        call["purpose"] == "equipment_gallery"
        for call in selector.calls
    )
    assert dispatcher.calls == [
        {
            "chat_id": _CHAT_ID,
            "material_id": 22,
            "trace_id": "trc-eq",
            "caption_override": None,
        }
    ]


@pytest.mark.asyncio
async def test_equipment_ask_no_material_no_dispatch_no_fabrication() -> None:
    selector = _SpySelector(picks={"equipment_gallery": None})
    dispatcher = _SpyDispatcher(outcomes=[])
    state_repo = _FakeStateRepo(
        initial={
            "chat_id": _CHAT_ID,
            "project_id": _PROJECT_ID,
            "current_stage": STAGE_PITCHING,
            "collected_intent": {
                "dates": "1 мая",
                "headcount": 4,
                "vehicle_count": 2,
                "difficulty": "начальный",
                "drivers": 1,
            },
        }
    )
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=_StubOpenRouter(),
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="что нужно из снаряжения?", ctx=_ctx()
    )
    # Equipment ask without a matched material is a no-op for media.
    assert dispatcher.calls == []
    # The answerer reports the equipment ask but does not lie about media.
    if result.text is not None:
        assert "фото" not in result.text.lower() or "коллег" in result.text.lower()


@pytest.mark.asyncio
async def test_equipment_phrase_что_нужно_dispatches() -> None:
    """The two-token phrase 'что нужно' fires when neither token is
    individually in the lemma set."""
    selector = _SpySelector(
        picks={"equipment_gallery": _material(material_id=33, kind="photo")}
    )
    dispatcher = _SpyDispatcher(outcomes=[{"ok": True}])
    state_repo = _FakeStateRepo(
        initial={
            "chat_id": _CHAT_ID,
            "project_id": _PROJECT_ID,
            "current_stage": STAGE_PITCHING,
            "collected_intent": {
                "dates": "1 мая",
                "headcount": 4,
                "vehicle_count": 2,
                "difficulty": "начальный",
                "drivers": 1,
            },
        }
    )
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=_StubOpenRouter(),
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="а что нужно с собой?", ctx=_ctx()
    )
    assert result.handled is True
    assert dispatcher.calls and dispatcher.calls[0]["material_id"] == 33


@pytest.mark.asyncio
async def test_selector_pick_exception_is_swallowed_no_dispatch() -> None:
    """A selector exception must not break the answerer's response path."""

    class _ExplodingSelector:
        calls: list[dict[str, Any]] = []

        def pick(self, **kwargs: Any) -> ClientMaterial | None:
            self.calls.append(kwargs)
            raise RuntimeError("repo down")

    dispatcher = _SpyDispatcher(outcomes=[])
    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Готово!",
        }
    )
    state_repo = _FakeStateRepo(initial=_scoping_state_with_4_fields())
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=__import__(
            "services.api.app.russian_text", fromlist=["get_russian_normalizer"]
        ).get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        material_selector=_ExplodingSelector(),
        material_dispatcher=dispatcher,
    )
    result = await answerer.try_answer(
        question="1 водитель", ctx=_ctx()
    )
    assert result.handled is True
    assert dispatcher.calls == []


@pytest.mark.asyncio
async def test_dispatcher_exception_appends_fallback_line() -> None:
    selector = _SpySelector(picks={"tour_preview": _material()})

    async def _exploding_dispatcher(**_: Any) -> dict[str, Any]:
        raise RuntimeError("api unreachable")

    openrouter = _StubOpenRouter()
    openrouter.queue_response(
        {
            "extracted_fields": {"drivers": 1},
            "next_question": "Принято.",
        }
    )
    state_repo = _FakeStateRepo(initial=_scoping_state_with_4_fields())
    answerer = SalesPersonaAnswerer(
        state_repo=state_repo,
        services_repo=_NoOpServicesRepo(),
        openrouter=openrouter,
        normalizer=__import__(
            "services.api.app.russian_text", fromlist=["get_russian_normalizer"]
        ).get_russian_normalizer(),
        clock=lambda: _NOW,
        bot_persona_getter=lambda: "Анна",
        material_selector=selector,
        material_dispatcher=_exploding_dispatcher,
    )
    result = await answerer.try_answer(
        question="1 водитель", ctx=_ctx()
    )
    assert result.handled is True
    text = result.text or ""
    assert "Принято" in text
    assert "позже" in text.lower() or "коллег" in text.lower()


@pytest.mark.asyncio
async def test_is_equipment_ask_empty_text_short_circuits() -> None:
    """Empty / whitespace-only text returns ``other`` (no dispatch)."""
    selector = _SpySelector(picks={"equipment_gallery": _material()})
    dispatcher = _SpyDispatcher(outcomes=[])
    state_repo = _FakeStateRepo(
        initial={
            "chat_id": _CHAT_ID,
            "project_id": _PROJECT_ID,
            "current_stage": STAGE_PITCHING,
            "collected_intent": {
                "dates": "1 мая",
                "headcount": 4,
                "vehicle_count": 2,
                "difficulty": "начальный",
                "drivers": 1,
            },
        }
    )
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=_StubOpenRouter(),
        state_repo=state_repo,
    )
    # Pure-punctuation message — yields no lemmas, no equipment match.
    await answerer.try_answer(question="???", ctx=_ctx())
    assert dispatcher.calls == []


def test_is_equipment_ask_directly_against_helper() -> None:
    from services.api.app.russian_text import get_russian_normalizer
    from services.api.app.sales.sales_persona_answerer import (
        _is_equipment_ask,
    )

    norm = get_russian_normalizer()
    assert _is_equipment_ask("", normalizer=norm) is False
    assert _is_equipment_ask("   ", normalizer=norm) is False
    assert _is_equipment_ask("???", normalizer=norm) is False
    assert _is_equipment_ask("какое снаряжение?", normalizer=norm) is True
    assert _is_equipment_ask("что нужно взять?", normalizer=norm) is True
    assert _is_equipment_ask("какая погода?", normalizer=norm) is False


@pytest.mark.asyncio
async def test_equipment_ask_dispatch_failure_appends_fallback() -> None:
    selector = _SpySelector(picks={"equipment_gallery": _material()})
    dispatcher = _SpyDispatcher(
        outcomes=[{"ok": False, "error_reason": "telegram_network_error"}]
    )
    state_repo = _FakeStateRepo(
        initial={
            "chat_id": _CHAT_ID,
            "project_id": _PROJECT_ID,
            "current_stage": STAGE_PITCHING,
            "collected_intent": {
                "dates": "1 мая",
                "headcount": 4,
                "vehicle_count": 2,
                "difficulty": "начальный",
                "drivers": 1,
            },
        }
    )
    answerer = _make_answerer(
        selector=selector,
        dispatcher=dispatcher,
        openrouter=_StubOpenRouter(),
        state_repo=state_repo,
    )
    result = await answerer.try_answer(
        question="что нужно из одежды?", ctx=_ctx()
    )
    assert result.handled is True
    assert "коллег" in (result.text or "").lower() or "позже" in (result.text or "").lower()
