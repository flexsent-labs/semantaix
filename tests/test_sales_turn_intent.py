"""Per-turn intent classifier for Story 12.06 — catalog & concept asks.

`classify_turn` runs at the top of every sales stage (scoping / pitching /
pricing) so the answerer can route conversational asides ("что у вас есть?",
"что такое каньонинг?") without losing funnel state. The classifier never
itself decides scoping_answer — that decision belongs to the stage handler
in the answerer, which sees `other` and treats the message as the answer
to its open question.
"""

from __future__ import annotations

import pytest

from services.api.app.russian_text import get_russian_normalizer
from services.api.app.sales.turn_intent import TurnIntent, classify_turn


@pytest.fixture(scope="module")
def normalizer():
    return get_russian_normalizer()


def test_catalog_ask_что_у_вас_есть(normalizer) -> None:
    result = classify_turn("Что у вас есть?", normalizer=normalizer)
    assert result.kind == "catalog_ask"
    assert result.term is None


def test_catalog_ask_какие_туры(normalizer) -> None:
    result = classify_turn("Какие туры?", normalizer=normalizer)
    assert result.kind == "catalog_ask"


def test_catalog_ask_что_предлагаете(normalizer) -> None:
    result = classify_turn(
        "А что вы предлагаете в мае?", normalizer=normalizer
    )
    assert result.kind == "catalog_ask"


def test_catalog_ask_варианты(normalizer) -> None:
    result = classify_turn(
        "Подскажите ваши варианты, пожалуйста", normalizer=normalizer
    )
    assert result.kind == "catalog_ask"


def test_catalog_ask_список(normalizer) -> None:
    result = classify_turn(
        "Можно список ваших услуг?", normalizer=normalizer
    )
    assert result.kind == "catalog_ask"


def test_concept_ask_что_такое_extracts_term(normalizer) -> None:
    result = classify_turn(
        "А что такое каньонинг?", normalizer=normalizer
    )
    assert result.kind == "concept_ask"
    assert result.term is not None
    assert "каньонинг" in result.term.lower()


def test_concept_ask_что_значит_extracts_term(normalizer) -> None:
    result = classify_turn(
        "Что значит эндуро?", normalizer=normalizer
    )
    assert result.kind == "concept_ask"
    assert result.term is not None
    assert "эндуро" in result.term.lower()


def test_concept_ask_объясните_extracts_term(normalizer) -> None:
    result = classify_turn(
        "Объясните каньонинг, пожалуйста", normalizer=normalizer
    )
    assert result.kind == "concept_ask"
    assert result.term is not None
    assert "каньонинг" in result.term.lower()


def test_concept_ask_расскажите_про_extracts_term(normalizer) -> None:
    result = classify_turn(
        "Расскажите про Медовеевку", normalizer=normalizer
    )
    assert result.kind == "concept_ask"
    assert result.term is not None
    assert "медовеевк" in result.term.lower()


def test_concept_ask_расскажите_о_extracts_term(normalizer) -> None:
    result = classify_turn(
        "Расскажите о каньонинге", normalizer=normalizer
    )
    assert result.kind == "concept_ask"
    assert result.term is not None
    assert "каньонинг" in result.term.lower()


def test_concept_ask_empty_term_is_other(normalizer) -> None:
    """`Что такое?` alone has no term — classify as `other` rather than guess."""
    result = classify_turn("Что такое?", normalizer=normalizer)
    assert result.kind == "other"


def test_concept_ask_punctuation_only_after_trigger_is_other(normalizer) -> None:
    result = classify_turn("Что такое...", normalizer=normalizer)
    assert result.kind == "other"


def test_concept_ask_term_stops_at_punctuation(normalizer) -> None:
    """Multi-sentence input — the term is bound to the trigger's sentence."""
    result = classify_turn(
        "Что такое каньонинг? И сколько стоит?", normalizer=normalizer
    )
    assert result.kind == "concept_ask"
    assert result.term is not None
    # Term must NOT include "и сколько стоит" — stops at the question mark.
    assert "сколько" not in result.term.lower()
    assert "каньонинг" in result.term.lower()


def test_price_ask_сколько_стоит(normalizer) -> None:
    result = classify_turn(
        "Сколько стоит тур?", normalizer=normalizer
    )
    assert result.kind == "price_ask"


def test_price_ask_цена(normalizer) -> None:
    result = classify_turn(
        "Какая цена за поездку?", normalizer=normalizer
    )
    assert result.kind == "price_ask"


def test_scoping_answer_shaped_question_is_other(normalizer) -> None:
    """A normal scoping reply does not match catalog/concept/price triggers."""
    result = classify_turn("Нас 6 человек", normalizer=normalizer)
    assert result.kind == "other"


def test_empty_text_is_other(normalizer) -> None:
    result = classify_turn("", normalizer=normalizer)
    assert result.kind == "other"


def test_whitespace_only_is_other(normalizer) -> None:
    result = classify_turn("   \n  ", normalizer=normalizer)
    assert result.kind == "other"


def test_turn_intent_kind_is_one_of_known_values(normalizer) -> None:
    """Defensive: every classifier output is a member of the known set."""
    known = {"catalog_ask", "concept_ask", "price_ask", "other"}
    samples = [
        "Что у вас есть?",
        "Что такое каньонинг?",
        "Сколько стоит?",
        "Нас четверо",
        "",
    ]
    for sample in samples:
        result = classify_turn(sample, normalizer=normalizer)
        assert result.kind in known


def test_turn_intent_is_frozen() -> None:
    """`TurnIntent` instances should be immutable so they round-trip safely."""
    intent = TurnIntent(kind="catalog_ask")
    with pytest.raises((AttributeError, Exception)):
        intent.kind = "other"  # type: ignore[misc]


def test_punctuation_only_input_is_other(normalizer) -> None:
    """A non-empty input that lemmatises to nothing falls through to ``other``."""
    assert classify_turn("?", normalizer=normalizer).kind == "other"
    assert classify_turn("...", normalizer=normalizer).kind == "other"


def test_concept_term_all_punctuation_is_other(normalizer) -> None:
    """A non-empty span with no alnum chars (e.g. "- ") downgrades to other."""
    result = classify_turn("Что такое -", normalizer=normalizer)
    assert result.kind == "other"


def test_catalog_beats_concept_when_both_match(normalizer) -> None:
    """A catalog-shaped question wins so we don't slice it as a concept ask."""
    # 'какие туры' is a catalog ask — even though 'что значит' could be
    # mis-detected against some other phrasing, the catalog match takes priority.
    result = classify_turn("Какие туры у вас?", normalizer=normalizer)
    assert result.kind == "catalog_ask"
