"""Customer-language detection for the sales persona (Story 12.45, D8/N3)."""

from __future__ import annotations

import pytest

from services.api.app.sales.reply_language import (
    detect_language,
    reply_language_directive,
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Hello! Can I book a buggy for 4 on June 4 at 11am? How much?", "en"),
        ("Здравствуйте!", "ru"),
        ("Можно 03.06 в 16:00 на багги, нас двое?", "ru"),
        ("Hi, хочу багги завтра", "ru"),  # more Cyrillic than Latin -> ru
        ("", "ru"),
        (None, "ru"),
    ],
)
def test_detect_language(text, expected) -> None:
    assert detect_language(text) == expected


def test_directive_is_english_instruction_for_english_text() -> None:
    directive = reply_language_directive("Hello, how much for a buggy?")
    assert directive  # non-empty
    assert "English" in directive


def test_directive_is_empty_for_russian_text() -> None:
    # Russian is the default — the prompt must be byte-identical (no regression).
    assert reply_language_directive("Здравствуйте, сколько стоит багги?") == ""
    assert reply_language_directive("") == ""
