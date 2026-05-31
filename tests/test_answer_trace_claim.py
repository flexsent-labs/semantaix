"""Story 12.24 — atomic inbound idempotency claim on ``AnswerTraceRepository``.

``claim_inbound`` is the lock that closes the in-flight window: the first caller
to see a ``trace_id`` wins (returns True); any concurrent/retried caller loses
(returns False) and is deduplicated by the endpoint instead of reprocessing and
re-sending the customer line.
"""

from __future__ import annotations

import pytest

from services.api.app.answer_trace import AnswerTraceRepository


def test_claim_inbound_first_wins_subsequent_lose(tmp_path):
    repo = AnswerTraceRepository(db_path=str(tmp_path / "traces.sqlite3"))
    assert repo.claim_inbound("trace-x") is True
    # Same trace_id again → already claimed (a retry / concurrent duplicate).
    assert repo.claim_inbound("trace-x") is False
    assert repo.claim_inbound("trace-x") is False
    # A different trace_id is independent.
    assert repo.claim_inbound("trace-y") is True


def test_claim_inbound_requires_trace_id(tmp_path):
    repo = AnswerTraceRepository(db_path=str(tmp_path / "traces.sqlite3"))
    with pytest.raises(ValueError):
        repo.claim_inbound("")


def test_claim_inbound_survives_reinit_same_db(tmp_path):
    # A claim persists across repository re-instantiation (named-volume / restart
    # parity): a retry after an api restart still dedups against the prior claim.
    db = str(tmp_path / "traces.sqlite3")
    assert AnswerTraceRepository(db_path=db).claim_inbound("trace-z") is True
    assert AnswerTraceRepository(db_path=db).claim_inbound("trace-z") is False
