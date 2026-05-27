"""Unit tests for ``parse_russian_date_span`` (Story 12.08 helper).

The proactive-followup job uses this to decide whether the customer's
stated tour date already lapsed. Only the shapes the scoping LLM emits
in practice are supported.
"""

from __future__ import annotations

from datetime import date

import pytest

from services.api.app.sales.russian_dates import parse_russian_date_span

_TODAY = date(2026, 5, 26)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 мая", date(2026, 5, 1)),
        ("20 апреля", date(2026, 4, 20)),
        ("10 июня", date(2026, 6, 10)),
        ("10-15 мая", date(2026, 5, 10)),
        ("с 1 по 5 мая", date(2026, 5, 1)),
        ("3 февраля", date(2026, 2, 3)),
        ("20.04.2026", date(2026, 4, 20)),
        ("20.04", date(2026, 4, 20)),
        ("20.04.26", date(2026, 4, 20)),
    ],
)
def test_parses_common_shapes(text: str, expected: date) -> None:
    assert parse_russian_date_span(text, today=_TODAY) == expected


def test_returns_none_for_empty() -> None:
    assert parse_russian_date_span("", today=_TODAY) is None
    assert parse_russian_date_span(None, today=_TODAY) is None
    assert parse_russian_date_span("   ", today=_TODAY) is None


def test_returns_none_for_unknown_month() -> None:
    assert parse_russian_date_span("20 фоо", today=_TODAY) is None


def test_returns_none_for_invalid_day() -> None:
    assert parse_russian_date_span("31 февраля", today=_TODAY) is None


def test_returns_none_for_no_match() -> None:
    assert parse_russian_date_span("через неделю", today=_TODAY) is None


def test_numeric_with_two_digit_year() -> None:
    assert parse_russian_date_span("20.04.27", today=_TODAY) == date(2027, 4, 20)


def test_numeric_invalid_returns_none() -> None:
    assert parse_russian_date_span("99.99", today=_TODAY) is None


def test_month_word_without_preceding_day_returns_none() -> None:
    # "майские праздники" — month word with no day-number before it.
    assert parse_russian_date_span("майские праздники", today=_TODAY) is None
