"""Fire-and-forget async usage ingestion seam (Story 14.02).

`UsageRecorder` accepts tracker events from LLM call sites and the message /
HITL instrumentation layers, queues them, and dispatches to the correct
repository on a single background consumer task.  The inbound path never
awaits the actual SQLite write — NFR-8 (usage-capture liveness over fidelity).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.api.app.usage.repositories import (
        UsageHitlEventRepository,
        UsageLlmCallRepository,
        UsageMessageRepository,
    )

_LOG = logging.getLogger(__name__)
_VALID_TRACKER_TYPES: frozenset[str] = frozenset({"llm", "messages", "hitl"})


class UsageRecorder:
    """Async fire-and-forget usage-event queue.

    Usage
    -----
    1. Construct with repo references.
    2. Call ``start()`` inside a FastAPI startup event (event loop is running).
    3. Inject into collaborators; they call ``await recorder.record(...)``.
    4. Call ``await recorder.aclose()`` in the shutdown event.
    """

    def __init__(
        self,
        *,
        llm_repo: "UsageLlmCallRepository",
        message_repo: "UsageMessageRepository",
        hitl_repo: "UsageHitlEventRepository",
        queue_maxsize: int = 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._llm_repo = llm_repo
        self._message_repo = message_repo
        self._hitl_repo = hitl_repo
        self._queue_maxsize = queue_maxsize
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=queue_maxsize)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._consumer_task: asyncio.Task | None = None
        self._closed = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the background consumer.  Must be called inside a running event loop.

        A fresh asyncio.Queue is created here (not in __init__) so that the
        queue is bound to the running event loop — each TestClient/container
        restart gets a queue on its own loop.  Items enqueued before start()
        (the window between module import and FastAPI startup) are intentionally
        discarded; no request-path record() calls occur in that window.
        """
        self._queue = asyncio.Queue(maxsize=self._queue_maxsize)
        self._closed = False
        self._consumer_task = asyncio.create_task(self._consumer_loop())
        _LOG.info("usage_recorder_started")

    async def record(
        self,
        *,
        tracker_type: str,
        project_id: int,
        payload: dict,
        trace_id: str | None = None,
    ) -> None:
        """Enqueue a tracker event.  Returns immediately; never awaits the write."""
        if self._closed:
            raise RuntimeError("UsageRecorder is closed")
        if tracker_type not in _VALID_TRACKER_TYPES:
            raise ValueError(f"Unknown tracker_type: {tracker_type!r}")
        item = {
            "tracker_type": tracker_type,
            "project_id": project_id,
            "payload": payload,
            "trace_id": trace_id,
        }
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                pass
            else:
                _LOG.warning(
                    "usage_queue_overflow_drop",
                    extra={"dropped_tracker_type": dropped["tracker_type"]},
                )
        await self._queue.put(item)

    async def aclose(self) -> None:
        """Drain remaining items then cancel the consumer task."""
        self._closed = True
        await self._queue.join()
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
        _LOG.info("usage_recorder_stopped")

    # ------------------------------------------------------------------
    # Background consumer
    # ------------------------------------------------------------------

    async def _consumer_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._dispatch(item)
            except Exception as exc:
                _LOG.warning(
                    "usage_record_failed",
                    extra={
                        "tracker_type": item.get("tracker_type"),
                        "trace_id": item.get("trace_id"),
                        "error_type": type(exc).__name__,
                    },
                )
            finally:
                self._queue.task_done()

    async def _dispatch(self, item: dict) -> None:
        from services.api.app.usage.repositories import (
            UsageHitlEventRow,
            UsageLlmCallRow,
            UsageMessageRow,
        )

        tracker_type = item["tracker_type"]
        project_id = item["project_id"]
        payload = item["payload"]

        trace_id = item.get("trace_id") or payload.get("trace_id")
        if tracker_type == "llm":
            row = UsageLlmCallRow(
                id=0,  # ignored on INSERT (auto-increment)
                project_id=project_id,
                model_name=payload.get("model_name", ""),
                prompt_tokens=payload.get("prompt_tokens", 0),
                completion_tokens=payload.get("completion_tokens", 0),
                cost_usd=payload.get("cost_usd"),
                call_outcome=payload.get("call_outcome", "error"),
                trace_id=trace_id,
                created_at=payload.get("created_at", ""),
            )
            await asyncio.to_thread(self._llm_repo.record, row)
        elif tracker_type == "messages":
            row = UsageMessageRow(
                id=0,
                project_id=project_id,
                direction=payload.get("direction", "in"),
                participant_role=payload.get("participant_role", "customer"),
                trace_id=trace_id,
                created_at=payload.get("created_at", ""),
            )
            await asyncio.to_thread(self._message_repo.record, row)
        elif tracker_type == "hitl":
            row = UsageHitlEventRow(
                id=0,
                project_id=project_id,
                event_type=payload.get("event_type", "created"),
                ticket_id=payload.get("ticket_id", 0),
                trace_id=trace_id,
                created_at=payload.get("created_at", ""),
            )
            await asyncio.to_thread(self._hitl_repo.record, row)
