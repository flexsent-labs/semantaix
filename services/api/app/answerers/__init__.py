from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnswerResult:
    handled: bool
    text: str | None = None
    response_mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnswerContext:
    chat_id: int | None
    customer_username: str | None
    trace_id: str
    now: datetime
    delivery_channel: str = "bot"
    operator_id: int | None = None
    language: str = "ru"
    country_code: str = "RU"
    timezone: str = "Europe/Moscow"
    location: str = "Moscow"
    grounding_threshold: float = 0.6
    project_id: int | None = None


class Answerer(Protocol):
    name: str

    async def try_answer(
        self, *, question: str, ctx: AnswerContext
    ) -> AnswerResult: ...


class AnswerPipeline:
    def __init__(self, answerers: list[Answerer]) -> None:
        self._answerers = list(answerers)

    @property
    def answerers(self) -> tuple[Answerer, ...]:
        return tuple(self._answerers)

    async def run(self, *, question: str, ctx: AnswerContext) -> AnswerResult:
        for answerer in self._answerers:
            result = await answerer.try_answer(question=question, ctx=ctx)
            logger.info(
                "answerer_evaluated",
                extra={
                    "trace_id": ctx.trace_id,
                    "answerer_name": answerer.name,
                    "handled": result.handled,
                    "response_mode": result.response_mode,
                },
            )
            if result.handled:
                metadata = dict(result.metadata)
                metadata.setdefault("answerer", answerer.name)
                return AnswerResult(
                    handled=True,
                    text=result.text,
                    response_mode=result.response_mode,
                    metadata=metadata,
                )
        logger.info(
            "answer_pipeline_no_handler",
            extra={
                "trace_id": ctx.trace_id,
                "evaluated_answerers": [a.name for a in self._answerers],
            },
        )
        return AnswerResult(handled=False)


__all__ = ["AnswerContext", "AnswerPipeline", "AnswerResult", "Answerer"]
