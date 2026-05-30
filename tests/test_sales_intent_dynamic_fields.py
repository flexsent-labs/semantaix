"""Story 12.15 — Intent carries arbitrary per-service fields (dict-backed).

The five transfer fields stay first-class attributes (back-compat), but a
per-service anketa may collect other keys (e.g. ``topic`` for a consultation).
Those round-trip through state via an ``extra`` map and merge only when the
active schema allows them.
"""

from __future__ import annotations

from services.api.app.sales.intent import Intent, intent_merge


def test_get_returns_legacy_and_custom_values() -> None:
    intent = Intent.from_dict({"dates": "завтра", "topic": "ипотека"})
    assert intent.get("dates") == "завтра"
    assert intent.get("topic") == "ипотека"
    assert intent.get("absent") is None


def test_to_dict_includes_and_round_trips_custom_fields() -> None:
    intent = Intent.from_dict({"dates": "завтра", "topic": "ипотека"})
    payload = intent.to_dict()
    assert payload["dates"] == "завтра"
    assert payload["topic"] == "ипотека"
    assert Intent.from_dict(payload).get("topic") == "ипотека"


def test_missing_fields_supports_custom_keys() -> None:
    intent = Intent.from_dict({"topic": "ипотека"})
    assert intent.missing_fields(("topic", "slot")) == ["slot"]
    assert intent.is_complete(("topic",))


def test_with_field_sets_custom_and_legacy() -> None:
    intent = Intent(dates="завтра")
    assert intent.with_field("topic", "x").get("topic") == "x"
    assert intent.with_field("headcount", 3).headcount == 3
    assert intent.get("topic") is None  # original untouched


def test_intent_merge_allowed_routes_custom_field() -> None:
    intent = Intent(dates="завтра")
    merged = intent_merge(intent, {"topic": "ипотека"}, allowed=("dates", "topic"))
    assert merged.get("topic") == "ипотека"
    assert merged.get("dates") == "завтра"  # preserved


def test_intent_merge_without_allowed_ignores_custom_field() -> None:
    # Back-compat: the default path still merges only the canonical five.
    merged = intent_merge(Intent(), {"topic": "ипотека", "dates": "завтра"})
    assert merged.get("dates") == "завтра"
    assert merged.get("topic") is None
