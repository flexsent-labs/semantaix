"""Unit matrix for the mid-scoping decline detector (Story 12.11)."""

from __future__ import annotations

import pytest

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.decline import is_decline

_N = get_russian_normalizer()


@pytest.mark.parametrize(
    "text",
    [
        "не нужно",
        "нет",
        "0",
        "не нужно водителей",
        "без водителей",
        "нисколько",
        "не требуется",
        "не надо",
        "нету",
    ],
)
def test_pure_declines_are_detected(text: str) -> None:
    assert is_decline(text, normalizer=_N) is True


@pytest.mark.parametrize(
    "text",
    [
        "нет, троих",     # negation + a real count → NOT a decline
        "двое",
        "сами",
        "не знаю",         # "I don't know" ≠ "doesn't apply"
        "трое водителей",
        "средняя сложность",
        "нужно",          # only a "need" filler, no negation → not a decline
        "водителей",      # only the field name → no signal
        "",
        "   ",
    ],
)
def test_real_answers_and_unknowns_are_not_declines(text: str) -> None:
    assert is_decline(text, normalizer=_N) is False
