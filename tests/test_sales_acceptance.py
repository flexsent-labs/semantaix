"""Edge-case tests for ``is_acceptance`` (Story 12.07).

Single-lemma overlap against the file-loaded acceptance list. The
production tests in ``test_sales_persona_answerer_acceptance.py`` cover
the affirmative path; this module locks the negative / edge branches.
"""

from __future__ import annotations

from pathlib import Path

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.acceptance import (
    is_acceptance,
    load_acceptance_phrases,
)


def test_empty_text_is_not_acceptance() -> None:
    normalizer = get_russian_normalizer()
    assert is_acceptance("", normalizer=normalizer) is False


def test_whitespace_only_is_not_acceptance() -> None:
    normalizer = get_russian_normalizer()
    assert is_acceptance("   ", normalizer=normalizer) is False


def test_punctuation_only_is_not_acceptance() -> None:
    normalizer = get_russian_normalizer()
    # razdel lemmas would be empty; the helper must short-circuit.
    assert is_acceptance("???", normalizer=normalizer) is False


def test_negation_is_not_acceptance() -> None:
    normalizer = get_russian_normalizer()
    assert is_acceptance("нет", normalizer=normalizer) is False


def test_loaded_phrases_include_canonical_entries() -> None:
    phrases = load_acceptance_phrases()
    assert "да" in phrases
    assert "согласен" in phrases
    assert "хорошо" in phrases


def test_empty_phrases_file_disables_detection(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("# only comments\n\n", encoding="utf-8")
    normalizer = get_russian_normalizer()
    assert (
        is_acceptance("да", normalizer=normalizer, phrases_path=str(empty))
        is False
    )


def test_phrases_file_with_only_unparseable_lines_disables_detection(
    tmp_path: Path,
) -> None:
    # razdel/pymorphy3 reduce these to no lemmas.
    junk = tmp_path / "junk.txt"
    junk.write_text("???\n!!!\n", encoding="utf-8")
    normalizer = get_russian_normalizer()
    assert (
        is_acceptance("да", normalizer=normalizer, phrases_path=str(junk))
        is False
    )
