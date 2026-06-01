"""Story 12.31 — webhook-entry idempotency on the Telegram ``update_id``.

Telegram redelivers a webhook when the handler returns non-200 or exceeds its
~5s deadline; every redelivery reuses the same ``update_id``. The customer path
is already idempotent (Story 12.24: the gateway's ``UNIQUE(source_message_id)``
persist dedup plus the api's atomic ``claim_inbound``), but operator-command
handlers — persona, ``/hitl_config``, the file library, and especially the slow
NL-service handler that makes an OpenRouter call — run in ``telegram_webhook``
*before* that persist dedup. A redelivery of a slow operator command would
therefore re-run the handler and double-act.

``WebhookUpdateClaimRepository`` closes that window. It mirrors the api-side
``AnswerTraceRepository.claim_inbound`` pattern: an atomic ``INSERT OR IGNORE``
where ``cursor.rowcount == 1`` means THIS delivery won the claim. The very top
of the webhook claims the ``update_id`` before any handler; a redelivery loses
the claim and is dropped immediately.

The store is deliberately *dedicated and non-transcript* — keyed on
``update_id`` in its own ``webhook_update_claims`` table in its own DB file,
never the ``messages``/``conversations`` transcript that feeds
``/knowledge/extract``. (Persisting the message first would leak
operator-command text into knowledge extraction — the reason the naive "persist
before the handlers" fix was ruled out.)
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def init_schema(db_path: str) -> None:
    with _connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_update_claims (
                update_id INTEGER PRIMARY KEY,
                claimed_at TEXT NOT NULL
            )
            """
        )


class WebhookUpdateClaimRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        init_schema(db_path)

    def claim(self, update_id: int) -> bool:
        """Atomically claim ``update_id`` at webhook entry.

        Returns ``True`` when THIS delivery won the claim (first to see the
        update), ``False`` when it was already claimed — i.e. a Telegram
        redelivery the webhook must drop instead of re-running its handlers.
        The claim persists (named-volume / restart parity) so a redelivery
        arriving after a bot_gateway restart still dedups.
        """
        # Re-init so a reassigned ``db_path`` (per-test isolation) still has the
        # table, mirroring ``AnswerTraceRepository.claim_inbound``.
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO webhook_update_claims (update_id, claimed_at) "
                "VALUES (?, ?)",
                (update_id, _now_iso()),
            )
            return cursor.rowcount == 1
