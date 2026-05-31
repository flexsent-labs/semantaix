"""Cancellation-request detection — "хочу отменить бронь".

Lemma-based, no LLM (sibling of :mod:`decline`). A cancellation is its own
intent and must be recognised BEFORE the booking funnel: the funnel's positive
seeds (бронь / запись) would otherwise pull "хочу отменить запись" into scoping,
and a bare "можно отменить?" — which is not a booking intent — would fall
through to generic RAG/HITL. Both previously surfaced the canned "передам
коллегам" handoff instead of a cancellation-specific reply.

Matched on the explicit cancellation lemmas ONLY (отменить / отменять / отмена)
so booking words (бронь, запись) and a mid-scoping field decline (decline.py:
"без водителей") are never swallowed.
"""

from __future__ import annotations

from typing import Protocol


class _Normalizer(Protocol):
    def lemmas(self, text: str) -> list[str]: ...


# pymorphy3 normal forms: perfective "отменить" (отмените / отменю / отменим),
# imperfective "отменять", and the noun "отмена". Intentionally narrow — no
# бронь / запись / отказаться / снять, which would collide with new-booking
# intent or a field decline.
_CANCEL_LEMMAS: frozenset[str] = frozenset({"отменить", "отменять", "отмена"})


def is_cancellation(text: str, *, normalizer: _Normalizer) -> bool:
    """True iff the message asks to cancel a booking."""
    if not text or not text.strip():
        return False
    return any(lemma in _CANCEL_LEMMAS for lemma in normalizer.lemmas(text))


__all__ = ["is_cancellation"]
