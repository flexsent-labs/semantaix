"""Out-of-scope request detection — "посоветуйте ресторан / отель".

Lemma-based, no LLM (sibling of :mod:`cancel_intent` / :mod:`decline`). The
buggy persona sells buggy rides; a request for a separate service (a restaurant
recommendation, a hotel) must be politely declined and redirected — never pulled
into the booking funnel and answered with the booking-acceptance handoff.

Deliberately CONSERVATIVE: matches only unambiguous dining/lodging venue nouns
that never appear in a buggy-booking field answer, so it cannot misfire on a
real booking turn. The caller additionally gates it on ``not is_sales_intent``
(a mixed "хочу багги … и ресторан?" stays in the funnel). It is extensible —
add lemmas here as new out-of-scope categories surface. A fully-general scope
classifier (car rental, weather, arbitrary chit-chat) is a larger follow-up.
"""

from __future__ import annotations

from typing import Protocol


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


# pymorphy3 normal forms — dining / lodging venues only. No buggy/booking words
# (багги, прокат, дата, …) and no field-answer values, so a real booking turn
# is never swallowed.
_OUT_OF_SCOPE_LEMMAS: frozenset[str] = frozenset(
    {
        # Dining / lodging venues.
        "ресторан",
        "кафе",
        "кафешка",
        "кофейня",
        "столовая",
        "бар",
        "шашлычная",
        "банкет",
        "отель",
        "гостиница",
        "хостел",
        "ночлег",
        # Story 12.93 (round-27 R27-1) — vehicles / activities the buggy persona
        # does NOT offer (aircraft, watercraft). A request to "fly a helicopter"
        # or "rent a yacht" must be declined and redirected, never pulled into the
        # buggy booking funnel and answered with the booking-acceptance handoff.
        # Conservative: only unambiguous non-offered modes (no багги / квадроцикл).
        "вертолёт",
        "вертолет",
        "самолёт",
        "самолет",
        "яхта",
        "катер",
        "лодка",
        "параплан",
        "парашют",
        "дельтаплан",
    }
)


def is_out_of_scope(text: str, *, normalizer: _Normalizer) -> bool:
    """True iff the message asks about a separate service the persona doesn't
    offer (dining / lodging, or a non-buggy vehicle like a helicopter / yacht)."""
    if not text or not text.strip():
        return False
    return any(lemma in _OUT_OF_SCOPE_LEMMAS for lemma in normalizer.lemmas(text))


__all__ = ["is_out_of_scope"]
