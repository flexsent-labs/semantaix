"""Browser-timezone boundary tests — Story 14.06.

Verifies that local_window_to_utc_range() produces correct UTC date ranges
when the admin's local time straddles a UTC day boundary.
"""
from __future__ import annotations

from datetime import date

from services.web_ui.app.usage_dashboard import local_window_to_utc_range


def test_msk_2am_1d_window_uses_previous_utc_day():
    """Admin in Moscow (UTC+3) at 02:00 MSK sees local today = 2026-05-26.

    Local midnight 2026-05-26 00:00 MSK = 2026-05-25 21:00 UTC.
    So utc_today = 2026-05-25; 1d window = yesterday = 2026-05-24.
    """
    local_date = date(2026, 5, 26)
    from_utc, to_utc = local_window_to_utc_range("1d", local_date, utc_offset_minutes=180)
    assert from_utc == date(2026, 5, 24)
    assert to_utc == date(2026, 5, 24)


def test_utc_1d_window():
    """Admin in UTC sees standard UTC day."""
    local_date = date(2026, 5, 26)
    from_utc, to_utc = local_window_to_utc_range("1d", local_date, utc_offset_minutes=0)
    assert from_utc == date(2026, 5, 25)
    assert to_utc == date(2026, 5, 25)


def test_utc_plus_5_1w_window():
    """UTC+5: local_date 2026-05-26 → utc_today 2026-05-25; 1w = [2026-05-18, 2026-05-24]."""
    local_date = date(2026, 5, 26)
    from_utc, to_utc = local_window_to_utc_range("1w", local_date, utc_offset_minutes=300)
    assert from_utc == date(2026, 5, 18)
    assert to_utc == date(2026, 5, 24)


def test_utc_minus_5_1d_window():
    """UTC-5: local_date 2026-05-26 → utc_today 2026-05-26; 1d = yesterday = 2026-05-25."""
    local_date = date(2026, 5, 26)
    from_utc, to_utc = local_window_to_utc_range("1d", local_date, utc_offset_minutes=-300)
    assert from_utc == date(2026, 5, 25)
    assert to_utc == date(2026, 5, 25)


def test_1m_window_caps_at_30_days():
    local_date = date(2026, 5, 26)
    from_utc, to_utc = local_window_to_utc_range("1m", local_date, utc_offset_minutes=0)
    assert (to_utc - from_utc).days == 29  # 30-day window → 29 days gap
