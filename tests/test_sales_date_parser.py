"""Unit tests for ``parse_russian_date_span`` (Story 12.07)."""

from __future__ import annotations

from datetime import date

import pytest

from services.api.app.sales.date_parser import parse_russian_date_span

_NOW = date(2026, 5, 1)


def test_parses_single_day_may() -> None:
    assert parse_russian_date_span("1 мая", now=_NOW) == (
        date(2026, 5, 1),
        date(2026, 5, 1),
    )


def test_parses_single_day_june() -> None:
    assert parse_russian_date_span("15 июня", now=_NOW) == (
        date(2026, 6, 15),
        date(2026, 6, 15),
    )


def test_parses_en_dash_range() -> None:
    assert parse_russian_date_span("1–3 мая", now=_NOW) == (
        date(2026, 5, 1),
        date(2026, 5, 3),
    )


def test_parses_hyphen_range() -> None:
    assert parse_russian_date_span("1-3 мая", now=_NOW) == (
        date(2026, 5, 1),
        date(2026, 5, 3),
    )


def test_parses_whole_month_prepositional() -> None:
    assert parse_russian_date_span("в мае", now=_NOW) == (
        date(2026, 5, 1),
        date(2026, 5, 31),
    )


def test_parses_within_sentence() -> None:
    assert parse_russian_date_span(
        "хочу 2 мая на тур", now=_NOW
    ) == (date(2026, 5, 2), date(2026, 5, 2))


def test_rejects_relative_phrase() -> None:
    assert parse_russian_date_span("следующая суббота", now=_NOW) is None


def test_rejects_vague_phrase() -> None:
    assert parse_russian_date_span("скоро", now=_NOW) is None


def test_rejects_empty_text() -> None:
    assert parse_russian_date_span("", now=_NOW) is None


def test_rejects_none_text() -> None:
    assert parse_russian_date_span(None, now=_NOW) is None


def test_rejects_whitespace_only() -> None:
    assert parse_russian_date_span("   ", now=_NOW) is None


def test_invalid_day_rejected() -> None:
    # 32-е мая is not a real date — the parser refuses without raising.
    assert parse_russian_date_span("32 мая", now=_NOW) is None


def test_inverted_range_falls_back_to_single_day() -> None:
    # 5–3 мая (descending) — the inverted range is rejected, but the
    # parser then matches the trailing single-day pattern. Lenient
    # behaviour: a typo still yields a usable date rather than a
    # blanket failure.
    assert parse_russian_date_span("5-3 мая", now=_NOW) == (
        date(2026, 5, 3),
        date(2026, 5, 3),
    )


def test_whole_month_october_uses_31_day_count() -> None:
    assert parse_russian_date_span("в октябре", now=_NOW) == (
        date(2026, 10, 1),
        date(2026, 10, 31),
    )


def test_whole_month_february_2026_uses_28_days() -> None:
    assert parse_russian_date_span("в феврале", now=_NOW) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )


def test_unknown_month_word_rejected() -> None:
    # The verb "лечу" is not a month — must not be misread.
    assert parse_russian_date_span("в среду", now=_NOW) is None


@pytest.mark.parametrize(
    "text,expected_month",
    [
        ("1 января", 1),
        ("1 февраля", 2),
        ("1 марта", 3),
        ("1 апреля", 4),
        ("1 мая", 5),
        ("1 июня", 6),
        ("1 июля", 7),
        ("1 августа", 8),
        ("1 сентября", 9),
        ("1 октября", 10),
        ("1 ноября", 11),
        ("1 декабря", 12),
    ],
)
def test_each_month_genitive_form(text: str, expected_month: int) -> None:
    result = parse_russian_date_span(text, now=_NOW)
    assert result is not None
    assert result[0].month == expected_month
    assert result[0].day == 1
