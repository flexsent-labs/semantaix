"""Story 12.15 — the per-service scoping schema model + built-in presets."""

from __future__ import annotations

from services.api.app.sales.intent import _FIELD_NAMES
from services.api.app.sales.scoping_schema import (
    CONSULTATION_SCHEMA,
    TRANSFER_SCHEMA,
    ScopingField,
    ScopingSchema,
)


def test_schema_exposes_keys_required_numeric_and_questions() -> None:
    schema = ScopingSchema(
        (
            ScopingField("dates", "На какую дату?", kind="text", required=True),
            ScopingField("headcount", "Сколько человек?", kind="number", required=True),
            ScopingField("difficulty", "Сложность?", kind="text", required=False),
        )
    )
    assert schema.keys() == ("dates", "headcount", "difficulty")
    assert schema.required_keys() == ("dates", "headcount")
    assert schema.numeric_keys() == frozenset({"headcount"})
    assert schema.question_for("headcount") == "Сколько человек?"
    assert schema.question_for("absent") is None


def test_with_required_narrows_the_required_set() -> None:
    narrowed = TRANSFER_SCHEMA.with_required(("dates", "headcount"))
    assert narrowed.required_keys() == ("dates", "headcount")
    # The field list / questions are unchanged — only `required` flips.
    assert narrowed.keys() == TRANSFER_SCHEMA.keys()
    assert narrowed.question_for("vehicle_count") == "Сколько багги нужно?"


def test_transfer_schema_preserves_legacy_defaults() -> None:
    # Default (unconfigured) behaviour = all five transfer fields required,
    # matching the legacy `_FIELD_NAMES` fallback.
    assert TRANSFER_SCHEMA.keys() == _FIELD_NAMES
    assert TRANSFER_SCHEMA.required_keys() == _FIELD_NAMES
    assert TRANSFER_SCHEMA.numeric_keys() == frozenset(
        {"headcount", "vehicle_count", "drivers"}
    )
    assert TRANSFER_SCHEMA.question_for("vehicle_count") == "Сколько багги нужно?"


def test_consultation_schema_drops_transfer_questions() -> None:
    keys = CONSULTATION_SCHEMA.keys()
    assert "headcount" not in keys and "vehicle_count" not in keys
    assert "topic" in keys and "contact" in keys
    # A consultation still needs a date (feeds the calendar via the `dates` key).
    assert "dates" in CONSULTATION_SCHEMA.required_keys()
    assert CONSULTATION_SCHEMA.numeric_keys() == frozenset()
