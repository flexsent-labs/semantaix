"""Cancel-on-reply hook for the proactive followup queue (Story 12.08).

Called from ``/conversations/inbound`` before the answer pipeline runs.
A customer reply during the silent window cancels the pending nudge so a
reply arriving at the same instant the queue fires never produces a
duplicate notification.

The hook is intentionally a thin shim around
``FollowupQueueRepository.mark_cancelled_replied`` so the inbound route
can stay readable — a single import + single call site.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

logger = logging.getLogger(__name__)


class _CancelTarget(Protocol):
    def mark_cancelled_replied(
        self, chat_id: int, *, now: datetime
    ) -> int: ...


def maybe_cancel(
    *,
    repo: _CancelTarget,
    chat_id: int | None,
    now: datetime,
    trace_id: str | None = None,
) -> int:
    """Cancel pending follow-ups for ``chat_id`` and return rows touched.

    Returns ``0`` when ``chat_id`` is ``None`` so the inbound route can
    call the hook unconditionally — there is nothing to cancel for an
    anonymous request.
    """
    if chat_id is None:
        return 0
    cancelled = repo.mark_cancelled_replied(int(chat_id), now=now)
    if cancelled:
        logger.info(
            "sales_followup_cancelled_on_reply",
            extra={
                "trace_id": trace_id,
                "chat_id": int(chat_id),
                "cancelled_rows": cancelled,
            },
        )
    return cancelled


__all__ = ["maybe_cancel"]
