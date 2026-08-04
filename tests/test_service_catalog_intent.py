from __future__ import annotations

import pytest

from services.api.app.answerers.service_catalog_intent import (
    _is_ordered_subsequence,
    is_service_catalog_query,
)
from services.api.app.russian_text import get_russian_normalizer


@pytest.fixture
def normalizer():
    return get_russian_normalizer()


@pytest.mark.parametrize(
    "text",
    [
        "услуги",
        "варианты",
        "опции",
        "какие ещё услуги есть",
        "Какие услуги вы предлагаете?",
        "что ещё есть?",
        "я хочу купить, что можете предложить?",
        "что у вас есть",
        "что ещё можно купить",
        "какие есть варианты",
        "покажите ассортимент",
    ],
)
def test_positive_catalog_phrases(normalizer, text):
    assert is_service_catalog_query(text=text, normalizer=normalizer) is True


@pytest.mark.parametrize(
    "text",
    [
        "когда придёт мой возврат?",
        "какая погода завтра в Москве",
        "сколько стоит доставка",
        "",
        "   ",
    ],
)
def test_negative_non_catalog_phrases(normalizer, text):
    assert is_service_catalog_query(text=text, normalizer=normalizer) is False


def test_literal_substring_match(normalizer):
    # Trailing context does not break the literal substring pass.
    assert (
        is_service_catalog_query(
            text="Здравствуйте! Что можете предложить из новинок?",
            normalizer=normalizer,
        )
        is True
    )


def test_lemma_match_handles_inflection(normalizer, tmp_path):
    # Phrase file holds the canonical form; an inflected query still matches
    # via the ordered-subsequence lemma fallback (not a literal substring).
    phrases = tmp_path / "phrases.txt"
    phrases.write_text("какие варианты\n", encoding="utf-8")
    assert (
        is_service_catalog_query(
            text="а какими вариантами вы располагаете",
            normalizer=normalizer,
            phrases_path=str(phrases),
        )
        is True
    )


def test_phrases_path_override_no_match(normalizer, tmp_path):
    phrases = tmp_path / "phrases.txt"
    phrases.write_text("только это\n", encoding="utf-8")
    assert (
        is_service_catalog_query(
            text="совершенно другой вопрос",
            normalizer=normalizer,
            phrases_path=str(phrases),
        )
        is False
    )


def test_empty_lemma_phrase_is_not_a_match(normalizer, tmp_path):
    # A punctuation-only phrase lemmatizes to [], which must never match.
    phrases = tmp_path / "phrases.txt"
    phrases.write_text("%%%\n", encoding="utf-8")
    assert (
        is_service_catalog_query(
            text="расскажите про доставку",
            normalizer=normalizer,
            phrases_path=str(phrases),
        )
        is False
    )


def test_is_ordered_subsequence_helper():
    assert _is_ordered_subsequence(["a", "c"], ["a", "b", "c"]) is True
    assert _is_ordered_subsequence(["c", "a"], ["a", "b", "c"]) is False
    assert _is_ordered_subsequence([], ["a", "b"]) is False
