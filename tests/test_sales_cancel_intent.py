"""Story 12.27 — cancellation-request detector (`is_cancellation`).

Lemma-based, no LLM. Catches the explicit cancellation lemmas
(отменить / отменять / отмена) without swallowing new-booking words
(бронь / запись) or a mid-scoping field decline (без водителей).
"""

from __future__ import annotations

import pytest

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.cancel_intent import is_cancellation

_N = get_russian_normalizer()


@pytest.mark.parametrize(
    "text",
    [
        "как отменить бронь?",
        "хочу отменить запись",
        "можно отменить?",
        "отмените мою бронь пожалуйста",
        "отмена",
        "хочу отменить",
        "давайте отменим",
    ],
)
def test_cancellation_phrases_detected(text: str) -> None:
    assert is_cancellation(text, normalizer=_N) is True


@pytest.mark.parametrize(
    "text",
    [
        "хочу забронировать багги",  # a NEW booking, not a cancellation
        "без водителей",  # a scoping decline (decline.py owns this)
        "сколько стоит?",  # a price ask
        "завтра в 14:00",  # a time
        "да, подтверждаю",  # an acceptance
        "",  # empty
        "   ",  # blank
    ],
)
def test_non_cancellation_not_detected(text: str) -> None:
    assert is_cancellation(text, normalizer=_N) is False
