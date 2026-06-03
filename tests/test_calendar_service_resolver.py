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
    extract_all_clocks,
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


# --- numeric absolute dates (Story 12.38, D10 #30) --------------------------
# The scoping LLM (and customers) sometimes emit numeric/ISO/slash dates rather
# than Russian month words: "20.06 в 13:00", "01.06.2026", "2026-09-15", "20/06".
# Those must reach the busy check too — and crucially the dotted "DD.MM" must not
# be misread as a "HH.MM" clock (the _HH_MM regex used to greedily eat "03.06").


def test_parses_numeric_dotted_date_with_colon_time() -> None:
    # "20.06" is later this year -> current year; clock "13:00" not eaten by 20.06.
    result = extract_requested_start(
        text="Запишите на 20.06 в 13:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 20, 13, 0, tzinfo=MOSCOW)


def test_numeric_dotted_date_past_rolls_to_next_year() -> None:
    # _NOW is 2026-06-05; "02.06" already lapsed this year -> rolls to next year.
    result = extract_requested_start(
        text="02.06 в 11:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2027, 6, 2, 11, 0, tzinfo=MOSCOW)


def test_parses_dotted_date_with_explicit_4digit_year() -> None:
    # Explicit year is honored as-is (no rollover) even when already past.
    result = extract_requested_start(
        text="01.06.2026 в 14:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 1, 14, 0, tzinfo=MOSCOW)


def test_parses_dotted_date_with_explicit_2digit_year() -> None:
    result = extract_requested_start(
        text="01.07.27 в 9:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2027, 7, 1, 9, 0, tzinfo=MOSCOW)


def test_parses_iso_date_with_time() -> None:
    result = extract_requested_start(
        text="2026-09-15 в 9:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 9, 15, 9, 0, tzinfo=MOSCOW)


def test_parses_slash_date_with_time() -> None:
    result = extract_requested_start(
        text="20/06 в 13:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 20, 13, 0, tzinfo=MOSCOW)


def test_parses_slash_date_with_explicit_year() -> None:
    result = extract_requested_start(
        text="01/07/2027 в 10:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2027, 7, 1, 10, 0, tzinfo=MOSCOW)


def test_numeric_date_with_chasov_clock() -> None:
    # Strip the date first, then the час-form clock still resolves.
    result = extract_requested_start(
        text="20.06 в 13 часов", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 20, 13, 0, tzinfo=MOSCOW)


def test_numeric_date_without_time_is_none() -> None:
    # A bare date with no clock left after stripping -> None (conservative).
    assert (
        extract_requested_start(text="20.06", now=_NOW, project_tz=MOSCOW) is None
    )


def test_invalid_numeric_dotted_date_is_none() -> None:
    # 31.02 is not a real date; no rollover salvages it -> None.
    assert (
        extract_requested_start(text="31.02 в 10:00", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_invalid_explicit_year_numeric_date_is_none() -> None:
    assert (
        extract_requested_start(
            text="31.02.2026 в 10:00", now=_NOW, project_tz=MOSCOW
        )
        is None
    )


def test_explicit_numeric_date_without_time_is_none() -> None:
    # A fully-qualified date but no clock -> None (a booking needs both).
    assert (
        extract_requested_start(text="01.06.2026", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_invalid_iso_date_is_none() -> None:
    # Month 13 is invalid -> ISO branch yields None, nothing else matches.
    assert (
        extract_requested_start(
            text="2026-13-01 в 10:00", now=_NOW, project_tz=MOSCOW
        )
        is None
    )


def test_invalid_slash_date_is_none() -> None:
    assert (
        extract_requested_start(text="40/06 в 10:00", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_dotted_value_stays_time_when_no_separate_clock() -> None:
    # "завтра в 05.06": 05.06 is a *valid* date, but stripping it leaves no clock,
    # so it must read as the time 05:06 (tomorrow), NOT a date. Preserves the
    # existing dotted-time contract while only treating DD.MM as a date when a
    # separate clock is present.
    result = extract_requested_start(
        text="завтра в 05.06", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 5, 6, tzinfo=MOSCOW)


def test_relative_anchor_wins_over_numeric_date() -> None:
    # "завтра" present alongside a numeric date -> relative offset is used, and
    # the dotted date is stripped so the clock "10:00" is read (not "20.06").
    result = extract_requested_start(
        text="завтра, не 20.06, в 10:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 10, 0, tzinfo=MOSCOW)


# --- copy constants ---------------------------------------------------------


def test_clarifying_copy_constants_are_russian_nonempty() -> None:
    for copy in (CLARIFY_NO_SERVICE_NAMED, CLARIFY_NO_MATCH, CLARIFY_AMBIGUOUS):
        assert copy.strip()
        assert any(ord(ch) >= 1024 for ch in copy)  # contains Cyrillic
    assert "{options}" in CLARIFY_AMBIGUOUS


# --- Story 12.50 (round-11 R11-2): English date/time support -----------------
# The bot must parse English relative days, am/pm clocks, weekday and
# "<month> <day>" forms to the SAME tz-aware datetime as their Russian
# equivalents, so an English booking reaches the same busy check.
# _NOW is Friday 5 June 2026, noon Moscow.


def test_en_tomorrow_2pm() -> None:
    r = extract_requested_start(text="tomorrow at 2pm", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2026, 6, 6, 14, 0, tzinfo=MOSCOW)


def test_en_today_10am() -> None:
    r = extract_requested_start(text="today at 10am", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2026, 6, 5, 10, 0, tzinfo=MOSCOW)


def test_en_tomorrow_with_minutes_pm() -> None:
    r = extract_requested_start(text="tomorrow at 2:30pm", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2026, 6, 6, 14, 30, tzinfo=MOSCOW)


def test_en_noon_and_midnight_edges() -> None:
    assert extract_requested_start(
        text="tomorrow at 12pm", now=_NOW, project_tz=MOSCOW
    ) == datetime(2026, 6, 6, 12, 0, tzinfo=MOSCOW)
    assert extract_requested_start(
        text="tomorrow at 12am", now=_NOW, project_tz=MOSCOW
    ) == datetime(2026, 6, 6, 0, 0, tzinfo=MOSCOW)


def test_en_day_after_tomorrow() -> None:
    r = extract_requested_start(
        text="day after tomorrow at 8am", now=_NOW, project_tz=MOSCOW
    )
    assert r == datetime(2026, 6, 7, 8, 0, tzinfo=MOSCOW)


def test_en_weekday_resolves_to_next_occurrence() -> None:
    # Friday 5 June → next Monday is 8 June.
    r = extract_requested_start(text="on Monday at 9am", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2026, 6, 8, 9, 0, tzinfo=MOSCOW)


def test_en_month_day_order() -> None:
    r = extract_requested_start(text="June 7 at 11am", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2026, 6, 7, 11, 0, tzinfo=MOSCOW)


def test_en_day_month_order_with_ordinal_and_of() -> None:
    r = extract_requested_start(
        text="7th of June at 11:00", now=_NOW, project_tz=MOSCOW
    )
    assert r == datetime(2026, 6, 7, 11, 0, tzinfo=MOSCOW)


def test_en_month_day_in_past_rolls_to_next_year() -> None:
    r = extract_requested_start(text="January 3 at 10am", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2027, 1, 3, 10, 0, tzinfo=MOSCOW)


def test_en_time_without_day_is_none() -> None:
    assert extract_requested_start(text="at 2pm", now=_NOW, project_tz=MOSCOW) is None


def test_en_invalid_ampm_hour_is_none() -> None:
    # 14pm is nonsensical; conservative parser declines rather than guessing.
    assert extract_requested_start(
        text="tomorrow at 14pm", now=_NOW, project_tz=MOSCOW
    ) is None


def test_en_24h_clock_still_parses_with_en_day() -> None:
    r = extract_requested_start(text="tomorrow at 14:00", now=_NOW, project_tz=MOSCOW)
    assert r == datetime(2026, 6, 6, 14, 0, tzinfo=MOSCOW)


# --- Story 12.51 (round-11 R11-1): extract_all_clocks ------------------------


def test_extract_all_clocks_two_hh_mm_times() -> None:
    assert extract_all_clocks("именно в 12:00, а не в 08:00") == [(12, 0), (8, 0)]


def test_extract_all_clocks_ampm() -> None:
    assert extract_all_clocks("what about 10am or 2pm?") == [(10, 0), (14, 0)]


def test_extract_all_clocks_russian_hours_form() -> None:
    assert extract_all_clocks("давайте в 3 часа") == [(3, 0)]


def test_extract_all_clocks_invalid_ampm_dropped() -> None:
    assert extract_all_clocks("14pm") == []


def test_extract_all_clocks_dedupes() -> None:
    assert extract_all_clocks("в 12:00, ещё раз в 12:00") == [(12, 0)]


def test_extract_all_clocks_none() -> None:
    assert extract_all_clocks("без времени") == []


def test_en_non_month_word_is_not_a_date() -> None:
    # "<digit> <word>" where the word isn't a month → no date, no guess.
    assert extract_requested_start(
        text="5 cats at 10am", now=_NOW, project_tz=MOSCOW
    ) is None


def test_en_invalid_calendar_day_is_none() -> None:
    # A valid month with an impossible day declines (conservative).
    assert extract_requested_start(
        text="February 30 at 10am", now=_NOW, project_tz=MOSCOW
    ) is None
