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


# --- bare hours + ordinal dates (Story 12.20) -------------------------------


def test_bare_hour_with_relative_day() -> None:
    result = extract_requested_start(text="завтра в 8", now=_NOW, project_tz=MOSCOW)
    assert result == datetime(2026, 6, 6, 8, 0, tzinfo=MOSCOW)


def test_bare_hour_utra_is_am() -> None:
    result = extract_requested_start(
        text="завтра в 8 утра", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 8, 0, tzinfo=MOSCOW)


def test_bare_hour_vechera_is_pm() -> None:
    result = extract_requested_start(
        text="завтра в 8 вечера", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 6, 20, 0, tzinfo=MOSCOW)


def test_bare_hour_out_of_range_is_none() -> None:
    assert (
        extract_requested_start(text="завтра в 25", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_ordinal_day_with_clock() -> None:
    # "10-ое" with now=5 June → 10 June (future, this month).
    result = extract_requested_start(
        text="10-ое в 14:00", now=_NOW, project_tz=MOSCOW
    )
    assert result == datetime(2026, 6, 10, 14, 0, tzinfo=MOSCOW)


def test_ordinal_day_bare_hour() -> None:
    # The reported "<ordinal> в <hour>" shape.
    result = extract_requested_start(text="10-ое в 8", now=_NOW, project_tz=MOSCOW)
    assert result == datetime(2026, 6, 10, 8, 0, tzinfo=MOSCOW)


def test_ordinal_forms_go_and_chisla() -> None:
    for text in ("10-го в 9", "10 числа в 9"):
        result = extract_requested_start(text=text, now=_NOW, project_tz=MOSCOW)
        assert result == datetime(2026, 6, 10, 9, 0, tzinfo=MOSCOW), text


def test_ordinal_past_day_rolls_to_next_month() -> None:
    # "3-ое" with now=5 June → 3 June is past → 3 July.
    result = extract_requested_start(text="3-ое в 9", now=_NOW, project_tz=MOSCOW)
    assert result == datetime(2026, 7, 3, 9, 0, tzinfo=MOSCOW)


def test_ordinal_invalid_day_rolls_to_month_that_has_it() -> None:
    # 31 June does not exist → roll forward to 31 July.
    result = extract_requested_start(text="31-ое в 9", now=_NOW, project_tz=MOSCOW)
    assert result == datetime(2026, 7, 31, 9, 0, tzinfo=MOSCOW)


def test_ordinal_day_out_of_range_is_none() -> None:
    assert (
        extract_requested_start(text="40-ое в 9", now=_NOW, project_tz=MOSCOW)
        is None
    )


def test_ordinal_without_clock_is_none() -> None:
    assert (
        extract_requested_start(text="10-ое", now=_NOW, project_tz=MOSCOW) is None
    )


# --- copy constants ---------------------------------------------------------


def test_clarifying_copy_constants_are_russian_nonempty() -> None:
    for copy in (CLARIFY_NO_SERVICE_NAMED, CLARIFY_NO_MATCH, CLARIFY_AMBIGUOUS):
        assert copy.strip()
        assert any(ord(ch) >= 1024 for ch in copy)  # contains Cyrillic
    assert "{options}" in CLARIFY_AMBIGUOUS
