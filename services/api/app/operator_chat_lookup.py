"""Resolve a Telegram username to a chat_id for sending login codes.

Lookup order:
1. Most recent ``operator_files`` row whose ``username`` matches — this is the
   chat_id the bot has been DM'ing for uploads.
2. ``operators`` table row whose ``username`` matches and whose ``chat_id`` is
   not NULL — written by the bot ``/hitl_config`` command.
3. Otherwise return None — the user has no known chat_id and login requests
   for them must be rejected with ``username_unknown_or_chat_id_missing``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def resolve_chat_id_for_username(
    *,
    username: str,
    operator_files_db_path: str,
    operators_db_path: str,
) -> int | None:
    chat_id = _from_operator_files(
        username=username, db_path=operator_files_db_path
    )
    if chat_id is not None:
        return chat_id
    return _from_operators(username=username, db_path=operators_db_path)


def _from_operator_files(*, username: str, db_path: str) -> int | None:
    if not Path(db_path).exists():
        return None
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        connection.execute("PRAGMA busy_timeout = 2000")
        row = connection.execute(
            """
            SELECT chat_id FROM operator_files
            WHERE username = ?
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (username,),
        ).fetchone()
    if row is None:
        return None
    return int(row["chat_id"])


def _from_operators(*, username: str, db_path: str) -> int | None:
    if not Path(db_path).exists():
        return None
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = 1")
        connection.execute("PRAGMA busy_timeout = 2000")
        row = connection.execute(
            "SELECT chat_id FROM operators WHERE username = ? AND chat_id IS NOT NULL LIMIT 1",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return int(row["chat_id"])
