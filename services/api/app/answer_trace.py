from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def _now() -> datetime:
    return datetime.now(UTC)


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
            CREATE TABLE IF NOT EXISTS answer_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                request_text TEXT NOT NULL,
                model_id TEXT,
                model_provider TEXT,
                latency_ms INTEGER,
                response_mode TEXT NOT NULL,
                guardrails_applied INTEGER NOT NULL,
                guardrail_outcome TEXT NOT NULL,
                guardrail_reasons TEXT NOT NULL,
                guardrail_score REAL,
                grounded INTEGER NOT NULL,
                no_retrieval_hit INTEGER NOT NULL,
                confidence REAL,
                retrieval_json TEXT NOT NULL,
                limitations_json TEXT NOT NULL,
                hitl_ticket_id INTEGER
            )
            """
        )
        # ALTER for pre-existing databases — keep idempotent for legacy installs.
        columns = [
            row[1]
            for row in connection.execute("PRAGMA table_info(answer_traces)").fetchall()
        ]
        if "hitl_ticket_id" not in columns:
            connection.execute(
                "ALTER TABLE answer_traces ADD COLUMN hitl_ticket_id INTEGER"
            )
        # Story 12.24 — inbound idempotency lock. Claimed at the START of
        # /conversations/inbound (before the slow pipeline + Telegram sends) so a
        # retried forward arriving while the first is still in-flight is
        # deduplicated instead of reprocessed (which re-sends the customer line).
        # Separate from answer_traces, whose row is written only at the END.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inbound_claims (
                trace_id TEXT PRIMARY KEY,
                claimed_at TEXT NOT NULL
            )
            """
        )


@dataclass(frozen=True)
class AnswerTrace:
    id: int
    trace_id: str
    created_at: str
    request_text: str
    model_id: str | None
    model_provider: str | None
    latency_ms: int | None
    response_mode: str
    guardrails_applied: bool
    guardrail_outcome: str
    guardrail_reasons: list[str]
    guardrail_score: float | None
    grounded: bool
    no_retrieval_hit: bool
    confidence: float | None
    retrieval: list[dict[str, object]]
    limitations: list[str]
    hitl_ticket_id: int | None = None


def _truncate_snippet(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


class AnswerTraceRepository:
    def __init__(self, *, db_path: str, snippet_max_chars: int = 240) -> None:
        self.db_path = db_path
        self.snippet_max_chars = snippet_max_chars
        init_schema(db_path)

    def write(
        self,
        *,
        trace_id: str,
        request_text: str,
        model_id: str | None,
        model_provider: str | None,
        latency_ms: int | None,
        response_mode: str,
        guardrails_applied: bool,
        guardrail_outcome: str,
        guardrail_reasons: list[str],
        guardrail_score: float | None,
        retrieval: list[dict[str, object]],
        confidence: float | None,
        limitations: list[str],
        hitl_ticket_id: int | None = None,
    ) -> AnswerTrace:
        if not trace_id:
            raise ValueError("trace_id_required")
        if not response_mode:
            raise ValueError("response_mode_required")
        init_schema(self.db_path)
        normalized_retrieval = [
            {
                "chunk_id": str(item.get("chunk_id", "")),
                "source_ref": str(item.get("source_ref", "")),
                "score": float(item.get("score", 0.0)),
                "text_snippet": _truncate_snippet(
                    str(item.get("text_snippet", "")), self.snippet_max_chars
                ),
            }
            for item in retrieval
        ]
        no_retrieval_hit = len(normalized_retrieval) == 0
        grounded = not no_retrieval_hit and guardrail_outcome == "valid"
        retrieval_json = json.dumps(normalized_retrieval, ensure_ascii=False, sort_keys=True)
        limitations_json = json.dumps(list(limitations), ensure_ascii=False)
        reasons_csv = ",".join(guardrail_reasons)
        with _connect(self.db_path) as connection:
            existing = connection.execute(
                "SELECT id FROM answer_traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if existing is not None:
                return self._fetch(connection, int(existing["id"]))
            cursor = connection.execute(
                """
                INSERT INTO answer_traces (
                    trace_id, created_at, request_text, model_id, model_provider,
                    latency_ms, response_mode, guardrails_applied, guardrail_outcome,
                    guardrail_reasons, guardrail_score, grounded, no_retrieval_hit,
                    confidence, retrieval_json, limitations_json, hitl_ticket_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    _now().isoformat(),
                    request_text,
                    model_id,
                    model_provider,
                    latency_ms,
                    response_mode,
                    1 if guardrails_applied else 0,
                    guardrail_outcome,
                    reasons_csv,
                    guardrail_score,
                    1 if grounded else 0,
                    1 if no_retrieval_hit else 0,
                    confidence,
                    retrieval_json,
                    limitations_json,
                    hitl_ticket_id,
                ),
            )
            return self._fetch(connection, int(cursor.lastrowid))

    def claim_inbound(self, trace_id: str) -> bool:
        """Atomically claim ``trace_id`` for inbound processing (Story 12.24).

        Returns ``True`` when THIS caller won the claim (first to see the
        trace), ``False`` when it was already claimed — i.e. a retry or a
        concurrent duplicate the endpoint must deduplicate instead of
        reprocessing. The claim persists (named-volume / restart parity); a
        genuine api-outage retry never reaches here (no 200 response), so the
        bot_gateway retry still delivers once the api is back.
        """
        if not trace_id:
            raise ValueError("trace_id_required")
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO inbound_claims (trace_id, claimed_at) "
                "VALUES (?, ?)",
                (trace_id, _now().isoformat()),
            )
            return cursor.rowcount == 1

    def get_by_trace_id(self, trace_id: str) -> AnswerTrace:
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, trace_id, created_at, request_text, model_id, model_provider,
                       latency_ms, response_mode, guardrails_applied, guardrail_outcome,
                       guardrail_reasons, guardrail_score, grounded, no_retrieval_hit,
                       confidence, retrieval_json, limitations_json, hitl_ticket_id
                FROM answer_traces
                WHERE trace_id = ?
                """,
                (trace_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"answer_trace_not_found:{trace_id}")
        return self._row_to_trace(row)

    def find_by_trace_id(self, trace_id: str) -> AnswerTrace | None:
        """Non-raising variant of get_by_trace_id used for idempotency checks."""
        try:
            return self.get_by_trace_id(trace_id)
        except LookupError:
            return None

    def list_traces(self, *, limit: int = 50) -> list[AnswerTrace]:
        if limit <= 0:
            return []
        init_schema(self.db_path)
        with _connect(self.db_path) as connection:
            rows = connection.execute(
                """
                SELECT id, trace_id, created_at, request_text, model_id, model_provider,
                       latency_ms, response_mode, guardrails_applied, guardrail_outcome,
                       guardrail_reasons, guardrail_score, grounded, no_retrieval_hit,
                       confidence, retrieval_json, limitations_json, hitl_ticket_id
                FROM answer_traces
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_trace(row) for row in rows]

    def _fetch(self, connection: sqlite3.Connection, row_id: int) -> AnswerTrace:
        row = connection.execute(
            """
            SELECT id, trace_id, created_at, request_text, model_id, model_provider,
                   latency_ms, response_mode, guardrails_applied, guardrail_outcome,
                   guardrail_reasons, guardrail_score, grounded, no_retrieval_hit,
                   confidence, retrieval_json, limitations_json, hitl_ticket_id
            FROM answer_traces
            WHERE id = ?
            """,
            (row_id,),
        ).fetchone()
        assert row is not None
        return self._row_to_trace(row)

    @staticmethod
    def _row_to_trace(row: sqlite3.Row) -> AnswerTrace:
        reasons_value = str(row["guardrail_reasons"]) if row["guardrail_reasons"] else ""
        retrieval = json.loads(str(row["retrieval_json"]))
        limitations = json.loads(str(row["limitations_json"]))
        return AnswerTrace(
            id=int(row["id"]),
            trace_id=str(row["trace_id"]),
            created_at=str(row["created_at"]),
            request_text=str(row["request_text"]),
            model_id=str(row["model_id"]) if row["model_id"] else None,
            model_provider=str(row["model_provider"]) if row["model_provider"] else None,
            latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
            response_mode=str(row["response_mode"]),
            guardrails_applied=bool(row["guardrails_applied"]),
            guardrail_outcome=str(row["guardrail_outcome"]),
            guardrail_reasons=[item for item in reasons_value.split(",") if item],
            guardrail_score=(
                float(row["guardrail_score"]) if row["guardrail_score"] is not None else None
            ),
            grounded=bool(row["grounded"]),
            no_retrieval_hit=bool(row["no_retrieval_hit"]),
            confidence=float(row["confidence"]) if row["confidence"] is not None else None,
            retrieval=list(retrieval),
            limitations=list(limitations),
            hitl_ticket_id=(
                int(row["hitl_ticket_id"]) if row["hitl_ticket_id"] is not None else None
            ),
        )
