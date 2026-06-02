"""Scope-guard answerer — last in pipeline.

Off-topic messages get a random short decline phrase (so they never reach HITL
and create operator noise). But an *in-scope* price/booking ask only reaches the
last resort when every upstream answerer skipped (e.g. the sales persona LLM
briefly failed) — declining a real customer with "Этим не занимаюсь." is a bug
(Story 12.39, D10). Those defer (skip) so the inbound endpoint escalates them to
a human instead.

Two guards keep that escalation precise:

1. **Project actually offers bookings/sales.** A fully-disabled noop project
   (the default — no calendar, no corpus) must keep declining a booking ask, not
   escalate it. The defer fires only when ``project_does_bookings`` is true
   (wired to calendar ``is_enabled`` in the live pipeline).
2. **PRECISE intent signals** — scheduling intent + a price/catalog turn kind —
   NOT the loose ``is_sales_intent`` seed match, which false-positives on factual
   questions ("Какое сегодня число?"). Out-of-scope asks (lodging/venue, Story
   12.34) are excluded so they still decline rather than escalate.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable

from services.api.app.answerers import AnswerContext, AnswerResult
from services.api.app.answerers.scheduling_context import has_scheduling_intent
from services.api.app.calendar.service_resolver import extract_requested_start
from services.api.app.russian_text import get_russian_normalizer
from services.api.app.russian_text.normalizer import RussianNormalizer
from services.api.app.sales.out_of_scope import is_out_of_scope
from services.api.app.sales.turn_intent import classify_turn

RESPONSE_MODE_SCOPE_DECLINE = "scope_decline"

# Turn kinds that mean "this is in-scope work the business does" — a price ask
# or a catalog/service-list ask. Booking/scheduling intent is detected
# separately via has_scheduling_intent.
_IN_SCOPE_TURN_KINDS = frozenset({"price_ask", "catalog_ask"})


class ScopeGuardAnswerer:
    name = "scope_guard"

    def __init__(
        self,
        *,
        phrases_getter: Callable[[], str],
        normalizer: RussianNormalizer | None = None,
        project_does_bookings: Callable[[int | None], bool] | None = None,
    ) -> None:
        self._phrases_getter = phrases_getter
        self._normalizer = normalizer or get_russian_normalizer()
        # True when the project offers booking/sales work, so an in-scope ask
        # that fell through to the last resort should escalate to a human rather
        # than decline. Default: no bookings -> preserve the plain decline path
        # (a disabled noop project must not turn off-topic into operator tickets).
        self._project_does_bookings = project_does_bookings or (lambda _project_id: False)

    def _pick_phrase(self) -> str:
        raw = self._phrases_getter()
        phrases = [p.strip() for p in raw.splitlines() if p.strip()]
        return random.choice(phrases) if phrases else raw.strip()

    def _is_in_scope(self, question: str, ctx: AnswerContext) -> bool:
        """True when ``question`` is an in-scope price/booking ask.

        Conservative + precise: an out-of-scope ask (Story 12.34) is never
        in-scope; otherwise a real scheduling intent, a price/catalog turn
        kind, OR a turn that parses as a concrete booking qualifies. Avoids
        ``is_sales_intent`` (too loose for the last resort — it fires on factual
        questions that should still decline).
        """
        if is_out_of_scope(question, normalizer=self._normalizer):
            return False
        if has_scheduling_intent(self._normalizer.normalize(question)):
            return True
        if classify_turn(question, normalizer=self._normalizer).kind in _IN_SCOPE_TURN_KINDS:
            return True
        # Story 12.39 (round-6 D10 #34): a booking expressed by STRUCTURE rather
        # than a scheduling verb — "Можно багги сегодня в 13:00, нас четверо" —
        # is missed by the verb-based has_scheduling_intent, but parses to a
        # concrete start. Treat a parseable date+time as an in-scope booking.
        return self._parses_as_booking(question, ctx)

    def _parses_as_booking(self, question: str, ctx: AnswerContext) -> bool:
        """True when ``question`` carries a concrete, parseable date + time.

        Only the presence (not the value) of the parsed slot matters here, so
        the exact project timezone is irrelevant — a tz-aware ``ctx.now`` is
        enough. A naive ``now`` (shouldn't happen in the live pipeline) is
        treated as un-parseable rather than guessed.
        """
        now = ctx.now
        if now is None or now.tzinfo is None:
            return False
        return (
            extract_requested_start(text=question, now=now, project_tz=now.tzinfo)
            is not None
        )

    async def try_answer(self, *, question: str, ctx: AnswerContext) -> AnswerResult:
        # Cheap intent check first; the project-config lookup (DB) only when the
        # ask is actually in-scope.
        if self._is_in_scope(question, ctx):
            does_bookings = await asyncio.to_thread(
                self._project_does_bookings, ctx.project_id
            )
            if does_bookings:
                # Defer to the inbound HITL escalation instead of declining a real
                # customer whose in-scope ask only reached us because upstream skipped.
                return AnswerResult(
                    handled=False,
                    metadata={"skip_reason": "in_scope_defer_to_hitl"},
                )
        return AnswerResult(
            handled=True,
            text=self._pick_phrase(),
            response_mode=RESPONSE_MODE_SCOPE_DECLINE,
        )
