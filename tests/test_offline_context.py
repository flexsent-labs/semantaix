from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.bot_gateway.app.offline_context import (
    build_inbound_text,
    is_stale,
    is_thin,
    load_context_cues,
)

_NOW = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
_CUES = frozenset({"да", "сколько", "а", "yes"})


def _epoch(dt: datetime) -> int:
    return int(dt.timestamp())


def test_is_stale_true_for_old_message() -> None:
    old = _epoch(_NOW - timedelta(minutes=10))
    assert is_stale(message_date=old, now=_NOW, stale_seconds=30) is True


def test_is_stale_false_for_recent_message() -> None:
    recent = _epoch(_NOW - timedelta(seconds=5))
    assert is_stale(message_date=recent, now=_NOW, stale_seconds=30) is False


def test_is_stale_false_at_exact_threshold() -> None:
    boundary = _epoch(_NOW - timedelta(seconds=30))
    # Exactly at the threshold is NOT stale (strict greater-than).
    assert is_stale(message_date=boundary, now=_NOW, stale_seconds=30) is False


def test_is_stale_false_when_date_missing() -> None:
    assert is_stale(message_date=None, now=_NOW, stale_seconds=30) is False


@pytest.mark.parametrize("bad", [10**20, -(10**20)])
def test_is_stale_false_for_out_of_range_epoch(bad: int) -> None:
    # Corrupt epoch from an untrusted payload must not raise nor buffer.
    assert is_stale(message_date=bad, now=_NOW, stale_seconds=30) is False


def test_is_thin_true_for_short_message() -> None:
    assert is_thin("да", min_context_chars=12, cues=_CUES) is True


def test_is_thin_true_for_blank_after_strip() -> None:
    # Length 0 < min, returns True before tokenising.
    assert is_thin("   ", min_context_chars=12, cues=_CUES) is True


def test_is_thin_true_when_no_word_tokens() -> None:
    # Long enough to pass the length gate but no \w tokens -> thin.
    assert is_thin("?!?!?!?!?!?!", min_context_chars=12, cues=_CUES) is True


def test_is_thin_true_when_exact_cue_phrase() -> None:
    # Long enough to pass length gate, whole normalised text is a cue.
    assert is_thin("СКОЛЬКО", min_context_chars=3, cues=_CUES) is True


def test_is_thin_true_when_first_word_is_cue() -> None:
    assert is_thin("а это вообще возможно?", min_context_chars=5, cues=_CUES) is True


def test_is_thin_false_for_self_contained_message() -> None:
    assert (
        is_thin(
            "Здравствуйте, у вас есть мужская стрижка?",
            min_context_chars=12,
            cues=_CUES,
        )
        is False
    )


def test_build_inbound_text_returns_latest_when_no_context() -> None:
    assert build_inbound_text(latest="да", preceding=[]) == "да"


def test_build_inbound_text_labels_preceding_context() -> None:
    text = build_inbound_text(
        latest="да",
        preceding=["Здравствуйте, есть стрижка?", "А сколько стоит?"],
    )
    assert text == (
        "Предыдущие сообщения (контекст):\n"
        "- Здравствуйте, есть стрижка?\n"
        "- А сколько стоит?\n"
        "Вопрос клиента: да"
    )


def test_load_context_cues_reads_default_file() -> None:
    cues = load_context_cues()
    assert "да" in cues
    assert "сколько" in cues
    # Comment + blank lines are skipped, entries are lowercased.
    assert "" not in cues
    assert all(c == c.lower() for c in cues)


def test_load_context_cues_reads_override_path(tmp_path) -> None:
    path = tmp_path / "cues.txt"
    path.write_text(
        "# comment\n\nДА\n  ок  \n",
        encoding="utf-8",
    )
    cues = load_context_cues(str(path))
    assert cues == frozenset({"да", "ок"})


@pytest.mark.parametrize("value", ["yes", "YES", " Yes "])
def test_is_thin_cue_matching_is_case_insensitive(value: str) -> None:
    assert is_thin(value, min_context_chars=12, cues=_CUES) is True
