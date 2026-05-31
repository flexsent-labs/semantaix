"""Unit matrix for Russian service resolution + conservative time extraction
(Epic 11, story 11.06).

Deterministic: ``extract_requested_start`` is always driven with a frozen
tz-aware ``now`` and a fixed ``project_tz``; resolution reuses the real
:class:`RussianNormalizer` (razdel + slang + pymorphy3) so inflected matching is
exercised end to end, not stubbed.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from services.api.app.calendar.service_resolver import (
    CLARIFY_AMBIGUOUS,
    CLARIFY_NO_MATCH,
    CLARIFY_NO_SERVICE_NAMED,
    Ambiguous,
    NoMatch,
    Resolved,
    extract_requested_start,
    resolve_service,
)
from services.api.app.calendar.settings_repository import ServiceRule
from services.api.app.russian_text.normalizer import RussianNormalizer

MOSCOW = ZoneInfo("Europe/Moscow")
# A Friday at noon, Moscow time, well clear of any holiday edge.
_NOW = datetime(2026, 6, 5, 12, 0, tzinfo=MOSCOW)


def _rule(rule_id: int, name: str | None) -> ServiceRule:
    return ServiceRule(
        id=rule_id,
        project_id=1,
        name=name,
        duration_minutes=60,
        working_hours=None,
        service_days=None,
        date_exceptions=None,
        updated_at=None,
    )


def _normalizer() -> RussianNormalizer:
    return RussianNormalizer()


# --- resolve_service --------------------------------------------------------


def test_exact_match_resolves() -> None:
    rules = [_rule(1, "маникюр"), _rule(2, "педикюр")]
    result = resolve_service(
        text="хочу маникюр", service_rules=rules, normalizer=_normalizer()
    )
    assert isinstance(result, Resolved)
    assert result.service.id == 1


def test_inflected_forms_resolve() -> None:
    normalizer = _normalizer()
    rules = [_rule(1, "маникюр")]
    for phrase in ("на маникюр", "маникюра", "запишите на маникюре"):
        result = resolve_service(
            text=phrase, service_rules=rules, normalizer=normalizer
        )
        assert isinstance(result, Resolved), phrase
        assert result.service.id == 1


def test_multiword_service_name_resolves() -> None:
    rules = [_rule(1, "маникюр гель"), _rule(2, "педикюр")]
    result = resolve_service(
        text="можно гель маникюр завтра?",
        service_rules=rules,
        normalizer=_normalizer(),
    )
    assert isinstance(result, Resolved)
    assert result.service.id == 1


def test_unknown_service_is_no_match() -> None:
    rules = [_rule(1, "маникюр"), _rule(2, "педикюр")]
    result = resolve_service(
        text="хочу подстричься", service_rules=rules, normalizer=_normalizer()
    )
    assert isinstance(result, NoMatch)


def test_two_overlapping_services_are_ambiguous() -> None:
    rules = [_rule(1, "маникюр"), _rule(2, "маникюр премиум")]
    # "маникюр премиум" requires both lemmas; mention both -> both match.
    result = resolve_service(
        text="запишите на маникюр премиум",
        service_rules=rules,
        normalizer=_normalizer(),
    )
    assert isinstance(result, Ambiguous)
    assert {c.id for c in result.candidates} == {1, 2}


def test_time_but_no_service_named_is_no_match() -> None:
    rules = [_rule(1, "маникюр"), _rule(2, "педикюр")]
    result = resolve_service(
        text="можно завтра в 15:00?",
        service_rules=rules,
        normalizer=_normalizer(),
    )
    assert isinstance(result, NoMatch)


def test_blank_and_unparseable_names_are_skipped() -> None:
    # None name, blank name, and a punctuation-only name all yield no lemmas.
    rules = [_rule(1, None), _rule(2, "   "), _rule(3, "!!!"), _rule(4, "маникюр")]
    result = resolve_service(
        text="хочу маникюр", service_rules=rules, normalizer=_normalizer()
    )
    assert isinstance(result, Resolved)
    assert result.service.id == 4


def test_no_rules_is_no_match() -> None:
    result = resolve_service(
        text="хочу маникюр", service_rules=[], normalizer=_normalizer()
    )
    assert isinstance(result, NoMatch)


# --- extract_requested_start ------------------------------------------------


def test_parses_zavtra_v_1500() -> None:
    result = extract_requested_start(
        text="завтра в 15:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 15, 0, tzinfo=MOSCOW)


def test_parses_v_subbotu_v_3_chasa() -> None:
    # Next Saturday from Friday 2026-06-05 is 2026-06-06.
    result = extract_requested_start(
        text="в субботу в 3 часа", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 3, 0, tzinfo=MOSCOW)


def test_parses_segodnya_with_chasov_form() -> None:
    result = extract_requested_start(
        text="сегодня в 9 часов", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 5, 9, 0, tzinfo=MOSCOW)


def test_parses_poslezavtra() -> None:
    result = extract_requested_start(
        text="послезавтра в 18:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 7, 18, 0, tzinfo=MOSCOW)


def test_weekday_today_resolves_to_same_day() -> None:
    # 2026-06-05 is a Friday; "в пятницу" with offset 0 stays today.
    result = extract_requested_start(
        text="в пятницу в 10:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 5, 10, 0, tzinfo=MOSCOW)


def test_dot_separated_time_parses() -> None:
    result = extract_requested_start(
        text="завтра в 15.30", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 15, 30, tzinfo=MOSCOW)


def test_time_without_day_is_none() -> None:
    assert extract_requested_start(text="в 15:00", now=_NOW, project_tz=MOSCOW) is None


def test_day_without_time_is_none() -> None:
    assert extract_requested_start(text="завтра", now=_NOW, project_tz=MOSCOW) is None


def test_no_day_no_time_is_none() -> None:
    assert (
        extract_requested_start(text="когда удобно", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_out_of_range_hour_minute_is_none() -> None:
    assert (
        extract_requested_start(text="завтра в 25:00", now=_NOW, project_tz=MOSCOW)
        is None
    )
    assert (
        extract_requested_start(text="завтра в 12:99", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_out_of_range_clock_hour_is_none() -> None:
    # "часов" form with an impossible hour also returns None (not 0:00 guess).
    assert (
        extract_requested_start(text="завтра в 30 часов", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_relative_word_wins_over_weekday() -> None:
    # Both "завтра" and "в субботу" present -> relative anchor (завтра) wins.
    result = extract_requested_start(
        text="завтра, а не в субботу, в 11:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 11, 0, tzinfo=MOSCOW)


def test_now_in_other_tz_is_converted_to_project_tz() -> None:
    # now given in UTC; project_tz Moscow. 2026-06-05 09:00 UTC == 12:00 MSK Fri.
    now_utc = datetime(2026, 6, 5, 9, 0, tzinfo=ZoneInfo("UTC"))
    result = extract_requested_start(
        text="завтра в 14:00", now=now_utc, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 14, 0, tzinfo=MOSCOW)


# --- absolute "<day> <month>" calendar dates --------------------------------
# Regression for the booking busy-check gap: the scoping LLM often resolves a
# relative reference ("в понедельник", "завтра") to an absolute date before it
# reaches the extractor. Those must parse too, else the slot skips the calendar
# check and is handed off as bookable even when the time is busy.


def test_parses_absolute_date_with_time() -> None:
    # _NOW is 2026-06-05; "2 июня" is earlier this year -> rolls to next year.
    result = extract_requested_start(
        text="2 июня в 11:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2027, 6, 2, 11, 0, tzinfo=MOSCOW)


def test_parses_absolute_date_later_this_year() -> None:
    result = extract_requested_start(
        text="на 15 сентября в 9:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 9, 15, 9, 0, tzinfo=MOSCOW)


def test_absolute_date_today_is_accepted() -> None:
    # Same calendar day as _NOW (5 июня) is not "past" — keep current year.
    result = extract_requested_start(
        text="5 июня в 18:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 5, 18, 0, tzinfo=MOSCOW)


def test_absolute_date_inflected_month_resolves() -> None:
    # Genitive "июля" matches by prefix; clock via the час-form.
    result = extract_requested_start(
        text="1 июля в 3 часа", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 7, 1, 3, 0, tzinfo=MOSCOW)


def test_absolute_date_without_time_is_none() -> None:
    assert (
        extract_requested_start(text="2 июня", now=_NOW, project_tz=MOSCOW) is None
    )


def test_day_with_non_month_word_is_none() -> None:
    # A day number followed by a non-month word is not a date.
    assert (
        extract_requested_start(
            text="нас 4 человека в 11:00", now=_NOW, project_tz=MOSCOW
        )
        is None
    )


def test_invalid_absolute_day_is_none() -> None:
    # 31 апреля is not a real date -> no rollover salvages it -> None.
    assert (
        extract_requested_start(
            text="31 апреля в 12:00", now=_NOW, project_tz=MOSCOW
        )
        is None
    )


def test_relative_anchor_still_wins_over_absolute_date() -> None:
    # "завтра" present alongside an absolute date -> relative offset is used.
    result = extract_requested_start(
        text="завтра, не 20 июня, в 10:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 10, 0, tzinfo=MOSCOW)


# --- copy constants ---------------------------------------------------------


def test_clarifying_copy_constants_are_russian_nonempty() -> None:
    for copy in (CLARIFY_NO_SERVICE_NAMED, CLARIFY_NO_MATCH, CLARIFY_AMBIGUOUS):
        assert copy.strip()
        assert any(ord(ch) >= 1024 for ch in copy)  # contains Cyrillic
    assert "{options}" in CLARIFY_AMBIGUOUS
