"""Render + dispatch a +1d proactive follow-up nudge (Story 12.08).

The fire handler is invoked by the api endpoint that the scheduler
``ProactiveFollowupJob`` calls. It owns the LLM render, the Telegram
send, and the queue state update — the endpoint is just a thin wrapper.

Failure paths:
  * LLM error or empty text → fall back to a hard-coded short Russian
    nudge so the customer always gets a real sentence.
  * Telegram error → mark the row ``skipped_stale`` with reason
    ``telegram_send_failed`` (the queue is single-shot — no retry).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from services.api.app.sales.followup_queue_repository import (
    REASON_TELEGRAM_SEND_FAILED,
    FollowupQueueRepository,
    FollowupRow,
)
from services.api.app.sales.intent import Intent

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parent / "system_prompts" / "sales_followup.txt"
_PROMPT_TEMPLATE = _PROMPT_PATH.read_text(encoding="utf-8")


class _OpenRouter(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        model: str | None = None,
    ) -> dict[str, Any]: ...


class _TelegramSender(Protocol):
    async def send_message(self, *, chat_id: int, text: str) -> int: ...


class _StateRepo(Protocol):
    def get(self, chat_id: int) -> dict[str, Any] | None: ...

    def upsert(self, **kwargs: Any) -> None: ...


@dataclass(frozen=True)
class FireOutcome:
    sent: bool
    fallback_text_used: bool
    text: str | None = None


def _format_intent(intent: Intent) -> str:
    items: list[str] = []
    for key, value in intent.to_dict().items():
        if value is None:
            continue
        items.append(f"{key}: {value}")
    return "; ".join(items) if items else "(намерение не собрано)"


def _resolve_customer_name(
    state: dict[str, Any] | None, fallback: str | None
) -> str:
    if state is not None:
        raw = state.get("customer_first_name") or state.get("customer_name")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    if fallback and fallback.strip():
        return fallback.strip()
    return ""


def _build_fallback_text(customer_name: str) -> str:
    """One-line nudge used when the LLM is unavailable or returns empty."""
    if customer_name:
        return f"{customer_name}, остались вопросы по туру?"
    return "Остались вопросы по туру?"


def _extract_text(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("text", "next_question", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if isinstance(payload, str) and payload.strip():
        return payload.strip()
    return ""


class FollowupFireHandler:
    """Coordinates render → send → mark-* for a single queue row."""

    def __init__(
        self,
        *,
        followup_repo: FollowupQueueRepository,
        state_repo: _StateRepo,
        openrouter: _OpenRouter,
        telegram_sender: _TelegramSender,
        persona_getter: Callable[[], str],
        clock: Callable[[], datetime],
    ) -> None:
        self._followup_repo = followup_repo
        self._state_repo = state_repo
        self._openrouter = openrouter
        self._telegram_sender = telegram_sender
        self._persona_getter = persona_getter
        self._clock = clock

    async def fire(
        self, row: FollowupRow, *, customer_name: str | None = None
    ) -> FireOutcome:
        state = await asyncio.to_thread(self._state_repo.get, row.chat_id)
        intent = Intent.from_dict((state or {}).get("collected_intent") or {})
        resolved_name = _resolve_customer_name(state, customer_name)
        text, fallback_used = await self._render(
            intent=intent, customer_name=resolved_name
        )

        try:
            await self._telegram_sender.send_message(
                chat_id=row.chat_id, text=text
            )
        except Exception as exc:  # broad: any send failure → skip-stale
            logger.warning(
                "sales_followup_send_failed",
                extra={
                    "followup_id": row.id,
                    "chat_id": row.chat_id,
                    "error": repr(exc),
                },
            )
            await asyncio.to_thread(
                self._followup_repo.mark_skipped_stale,
                row.id,
                reason=REASON_TELEGRAM_SEND_FAILED,
                now=self._clock(),
            )
            return FireOutcome(
                sent=False, fallback_text_used=fallback_used, text=text
            )

        sent_at = self._clock()
        await asyncio.to_thread(
            self._followup_repo.mark_sent, row.id, now=sent_at
        )
        try:
            await asyncio.to_thread(
                lambda: self._state_repo.upsert(
                    chat_id=int(row.chat_id),
                    project_id=int(row.project_id),
                    current_stage=str(
                        (state or {}).get("current_stage") or "scoping"
                    ),
                    collected_intent=intent.to_dict(),
                    now=sent_at,
                    last_bot_msg_at=sent_at,
                )
            )
        except Exception as exc:  # defensive — state write failure is non-fatal
            logger.warning(
                "sales_followup_state_update_failed",
                extra={
                    "followup_id": row.id,
                    "chat_id": row.chat_id,
                    "error": repr(exc),
                },
            )
        logger.info(
            "sales_followup_sent",
            extra={
                "followup_id": row.id,
                "chat_id": row.chat_id,
                "fallback_text_used": fallback_used,
            },
        )
        return FireOutcome(
            sent=True, fallback_text_used=fallback_used, text=text
        )

    async def _render(
        self, *, intent: Intent, customer_name: str
    ) -> tuple[str, bool]:
        persona = self._persona_getter()
        system = _PROMPT_TEMPLATE.format(
            persona=persona,
            intent_context=_format_intent(intent),
            customer_name=customer_name or "(не указано)",
        )
        user = "Сформируй сообщение для возвращения клиента в разговор."
        try:
            payload = await self._openrouter.complete_json(
                system=system, user=user
            )
        except Exception as exc:  # defensive — LLM transport
            logger.warning(
                "sales_followup_llm_error", extra={"error": repr(exc)}
            )
            return _build_fallback_text(customer_name), True

        text = _extract_text(payload)
        if not text:
            return _build_fallback_text(customer_name), True
        return text, False


__all__ = ["FireOutcome", "FollowupFireHandler"]
