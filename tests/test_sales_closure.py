"""Tests for ``is_no_more`` — closure / "nothing more" detection.

Mirrors ``is_acceptance`` (single-lemma overlap against a file-loaded
list). Consulted only in the ``pitching`` / completion context, where a
bare "всё" / "нет" means "I have nothing more to add" rather than a field
answer.
"""

from __future__ import annotations

from pathlib import Path

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.closure import (
    is_no_more,
    load_closure_phrases,
)


def test_vsyo_is_closure() -> None:
    normalizer = get_russian_normalizer()
    assert is_no_more("всё", normalizer=normalizer) is True


def test_nothing_more_phrase_is_closure() -> None:
    normalizer = get_russian_normalizer()
    assert is_no_more("больше ничего", normalizer=normalizer) is True


def test_net_is_closure() -> None:
    # In the pitching context a bare "нет" means "no more".
    normalizer = get_russian_normalizer()
    assert is_no_more("нет", normalizer=normalizer) is True


def test_unrelated_text_is_not_closure() -> None:
    normalizer = get_russian_normalizer()
    assert is_no_more("хочу ещё квадроцикл", normalizer=normalizer) is False


def test_empty_text_is_not_closure() -> None:
    normalizer = get_russian_normalizer()
    assert is_no_more("", normalizer=normalizer) is False


def test_whitespace_only_is_not_closure() -> None:
    normalizer = get_russian_normalizer()
    assert is_no_more("   ", normalizer=normalizer) is False


def test_punctuation_only_is_not_closure() -> None:
    normalizer = get_russian_normalizer()
    assert is_no_more("???", normalizer=normalizer) is False


def test_loaded_phrases_include_canonical_entries() -> None:
    phrases = load_closure_phrases()
    assert "всё" in phrases
    assert "ничего" in phrases
    assert "достаточно" in phrases


def test_empty_phrases_file_disables_detection(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    normalizer = get_russian_normalizer()
    assert (
        is_no_more("всё", normalizer=normalizer, phrases_path=str(empty))
        is False
    )


def test_phrases_file_with_only_unparseable_lines_disables_detection(
    tmp_path: Path,
) -> None:
    junk = tmp_path / "junk.txt"
    junk.write_text("???\n!!!\n", encoding="utf-8")
    normalizer = get_russian_normalizer()
    assert (
        is_no_more("всё", normalizer=normalizer, phrases_path=str(junk))
        is False
    )
