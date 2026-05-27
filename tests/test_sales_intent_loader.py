"""Tests for the sales intent loader (Story 12.03).

The loader wraps `RussianNormalizer.lemmas(text)` overlap against the seed
phrases in `data/russian_sales_intent.txt`. It is the always-on first stage
of the SalesPersonaAnswerer's activation gate when no state row exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.russian_sales_intent import (
    is_sales_intent,
    load_sales_intent_phrases,
)


@pytest.fixture
def normalizer():
    return get_russian_normalizer()


def test_seed_file_loads_and_trims(tmp_path: Path) -> None:
    file = tmp_path / "intent.txt"
    file.write_text(
        "# comment\n"
        "\n"
        "  тур  \n"
        "прокат\n"
        "# another comment\n"
        "цена\n",
        encoding="utf-8",
    )
    phrases = load_sales_intent_phrases(str(file))
    assert phrases == ("тур", "прокат", "цена")


def test_default_file_loads_and_is_non_empty() -> None:
    phrases = load_sales_intent_phrases()
    assert len(phrases) >= 5
    # No blank or comment lines should have survived the trim.
    for phrase in phrases:
        assert phrase.strip() == phrase
        assert not phrase.startswith("#")


def test_match_on_obvious_sales_phrase(normalizer) -> None:
    assert is_sales_intent("Хочу прокатиться на квадроцикле", normalizer=normalizer) is True


def test_match_on_referral_greeting(normalizer) -> None:
    # The opening message from the Дарья transcript — a referral with no
    # explicit verb-form lemma, but "тур" / "интересует" / "даты" hit.
    assert (
        is_sales_intent(
            "Здравствуйте, контакт передали из Хиллс. Какие даты у вас свободны?",
            normalizer=normalizer,
        )
        is True
    )


def test_no_match_on_unrelated_question(normalizer) -> None:
    assert (
        is_sales_intent("Какая сегодня погода в Москве?", normalizer=normalizer)
        is False
    )


def test_no_match_on_empty_text(normalizer) -> None:
    assert is_sales_intent("", normalizer=normalizer) is False
    assert is_sales_intent("   ", normalizer=normalizer) is False


def test_no_match_when_phrases_file_is_empty(tmp_path: Path, normalizer) -> None:
    file = tmp_path / "empty.txt"
    file.write_text("# comments only\n\n", encoding="utf-8")
    assert is_sales_intent(
        "Хочу тур", normalizer=normalizer, phrases_path=str(file)
    ) is False


def test_no_match_when_text_yields_no_lemmas(tmp_path: Path, normalizer) -> None:
    """Pure-punctuation text has no lemmas — the matcher must skip cleanly."""
    file = tmp_path / "phrases.txt"
    file.write_text("тур\n", encoding="utf-8")
    assert is_sales_intent(
        "???!!! ...", normalizer=normalizer, phrases_path=str(file)
    ) is False


def test_skips_phrases_that_lemmatize_to_nothing(
    tmp_path: Path, normalizer
) -> None:
    """A phrase that is pure punctuation lemmatizes to an empty list — the
    matcher must continue and try the next phrase, not crash."""
    file = tmp_path / "phrases.txt"
    file.write_text("...\nтур\n", encoding="utf-8")
    assert is_sales_intent(
        "Хочу тур", normalizer=normalizer, phrases_path=str(file)
    ) is True
