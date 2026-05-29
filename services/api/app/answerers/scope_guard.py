"""Scope-guard answerer — last in pipeline, always fires.

Picks a random short phrase from a configurable list and returns it as the
answer. Prevents off-topic messages from reaching HITL escalation without
creating operator noise.
"""

from __future__ import annotations

import random
from collections.abc import Callable

from services.api.app.answerers import AnswerContext, AnswerResult

RESPONSE_MODE_SCOPE_DECLINE = "scope_decline"


class ScopeGuardAnswerer:
    name = "scope_guard"

    def __init__(self, *, phrases_getter: Callable[[], str]) -> None:
        self._phrases_getter = phrases_getter

    def _pick_phrase(self) -> str:
        raw = self._phrases_getter()
        phrases = [p.strip() for p in raw.splitlines() if p.strip()]
        return random.choice(phrases) if phrases else raw.strip()

    async def try_answer(self, *, question: str, ctx: AnswerContext) -> AnswerResult:
        return AnswerResult(
            handled=True,
            text=self._pick_phrase(),
            response_mode=RESPONSE_MODE_SCOPE_DECLINE,
        )
