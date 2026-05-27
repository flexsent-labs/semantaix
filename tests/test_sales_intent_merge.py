"""Tests for `intent_merge` (Story 12.03).

The merge helper combines an existing typed `Intent` with the
`extracted_fields` dict that the LLM returned this turn. Per the story:
- never overwrite a populated field with `None`;
- replace a populated field only when the new turn explicitly carries a
  new non-None value;
- ignore absent keys (the LLM is instructed to omit fields it didn't see,
  not to send `null`).
"""

from __future__ import annotations

from services.api.app.sales.intent import Intent, intent_merge


def test_merge_into_empty_intent() -> None:
    base = Intent()
    merged = intent_merge(
        base,
        {"dates": "1 мая", "headcount": 6, "vehicle_count": 3},
    )
    assert merged.dates == "1 мая"
    assert merged.headcount == 6
    assert merged.vehicle_count == 3
    assert merged.difficulty is None
    assert merged.drivers is None


def test_merge_preserves_populated_when_new_turn_silent() -> None:
    base = Intent(dates="1 мая", headcount=6)
    merged = intent_merge(base, {"vehicle_count": 3})
    assert merged.dates == "1 мая"
    assert merged.headcount == 6
    assert merged.vehicle_count == 3


def test_merge_replaces_populated_when_new_value_present() -> None:
    base = Intent(dates="1 мая")
    merged = intent_merge(base, {"dates": "2 мая"})
    assert merged.dates == "2 мая"


def test_merge_ignores_explicit_none() -> None:
    # Defensive: if the LLM ignores the prompt and sends `null`, we must
    # NOT clobber an already-populated field.
    base = Intent(dates="1 мая", headcount=6)
    merged = intent_merge(base, {"dates": None, "headcount": None})
    assert merged.dates == "1 мая"
    assert merged.headcount == 6


def test_merge_ignores_unknown_keys() -> None:
    base = Intent()
    merged = intent_merge(base, {"unrelated": "ignored", "dates": "1 мая"})
    assert merged.dates == "1 мая"


def test_merge_returns_new_instance_does_not_mutate_input() -> None:
    base = Intent(dates="1 мая")
    merged = intent_merge(base, {"headcount": 6})
    assert merged is not base
    assert base.headcount is None  # original untouched


def test_merge_with_empty_extracted_returns_equivalent_intent() -> None:
    base = Intent(dates="1 мая", headcount=6)
    merged = intent_merge(base, {})
    assert merged == base


def test_intent_missing_fields_lists_unfilled_fields() -> None:
    intent = Intent(dates="1 мая", headcount=6)
    assert intent.missing_fields() == ["vehicle_count", "difficulty", "drivers"]


def test_intent_missing_fields_empty_when_all_set() -> None:
    intent = Intent(
        dates="1 мая",
        headcount=6,
        vehicle_count=3,
        difficulty="средний",
        drivers="мужчины 30+",
    )
    assert intent.missing_fields() == []


def test_intent_to_dict_round_trip() -> None:
    intent = Intent(dates="1 мая", headcount=6)
    payload = intent.to_dict()
    assert payload == {
        "dates": "1 мая",
        "headcount": 6,
        "vehicle_count": None,
        "difficulty": None,
        "drivers": None,
    }
    assert Intent.from_dict(payload) == intent


def test_intent_from_dict_ignores_unknown_keys() -> None:
    intent = Intent.from_dict({"dates": "1 мая", "extraneous": "x"})
    assert intent == Intent(dates="1 мая")


def test_intent_from_dict_with_none_values_round_trips() -> None:
    intent = Intent.from_dict(
        {"dates": None, "headcount": None, "vehicle_count": None}
    )
    assert intent == Intent()
