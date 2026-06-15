"""Per-update structured logs make it possible to confirm in a future
"only saw N of M files" report whether Telegram actually delivered every
webhook update."""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from services.bot_gateway.app import main as bot_main
from services.bot_gateway.app.main import app as bot_app
from services.bot_gateway.app.media_group_buffer import MediaGroupBuffer
from services.bot_gateway.app.operator_files import OperatorFileRepository


@pytest.fixture
def isolated_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(
        bot_main.settings, "persistence_db_path", str(tmp_path / "story.db")
    )
    monkeypatch.setattr(
        bot_main.settings, "hitl_ticket_db_path", str(tmp_path / "hitl.db")
    )
    monkeypatch.setattr(bot_main.settings, "telegram_bot_token", "TKN")

    class _StubHitlRepo:
        def get_runtime_config(self, key):
            return None

        def set_runtime_config(self, **kwargs):
            pass

        def list_all(self):
            return []

    monkeypatch.setattr(bot_main, "hitl_ticket_repository", _StubHitlRepo())

    from unittest.mock import AsyncMock
    monkeypatch.setattr(
        bot_main.api_client,
        "find_operator_by_username",
        AsyncMock(
            return_value={
                "username": "@ajdevy", "chat_id": 100, "project_id": 1, "is_active": True
            }
        ),
    )

    monkeypatch.setattr(
        bot_main,
        "operator_file_repository",
        OperatorFileRepository(str(tmp_path / "files.db")),
    )
    monkeypatch.setattr(
        bot_main, "media_group_buffer", MediaGroupBuffer(str(tmp_path / "hitl.db"))
    )

    sent: list = []

    async def fake_send_dm(chat_id, text):
        sent.append((chat_id, text))

    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)
    return sent


def _find(records, message: str):
    return [r for r in records if r.message == message]


def test_telegram_update_received_logged_with_full_envelope(
    isolated_bot, caplog
):
    payload = {
        "update_id": 12345,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "media_group_id": "MG-OBS-1",
            "document": {
                "file_id": "OBS-FID",
                "file_name": "x.pdf",
                "mime_type": "application/pdf",
                "file_size": 42,
            },
        },
    }
    client = TestClient(bot_app)
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.main"):
        client.post("/telegram/webhook", json=payload)

    records = _find(caplog.records, "telegram_update_received")
    assert len(records) == 1
    extra = records[0].__dict__
    assert extra["update_id"] == 12345
    assert extra["chat_id"] == 100
    assert extra["username"] == "@ajdevy"
    assert extra["has_text"] is False
    assert extra["caption_present"] is False
    assert extra["media_group_id"] == "MG-OBS-1"
    assert extra["attachment_count"] == 1
    assert extra["attachment_kinds"] == ["document"]
    assert extra["attachment_sizes"] == [42]


def test_telegram_update_received_logged_for_text_only(isolated_bot, caplog):
    payload = {
        "update_id": 99,
        "message": {
            "message_id": 9,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "text": "hello",
        },
    }
    client = TestClient(bot_app)
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.main"):
        client.post("/telegram/webhook", json=payload)
    records = _find(caplog.records, "telegram_update_received")
    assert records
    extra = records[0].__dict__
    assert extra["has_text"] is True
    assert extra["media_group_id"] is None
    assert extra["attachment_count"] == 0


def test_observability_log_route_decision_for_files_list(isolated_bot, caplog):
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "text": "/files",
        },
    }
    client = TestClient(bot_app)
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.main"):
        client.post("/telegram/webhook", json=payload)
    records = _find(caplog.records, "telegram_update_routed")
    assert records
    extra = records[-1].__dict__
    assert extra["route"] == "files_list"


def test_observability_log_for_kb_session_opened(isolated_bot, caplog):
    payload = {
        "update_id": 5,
        "message": {
            "message_id": 5,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "text": "хочу добавить материалы в knowledge base",
        },
    }
    client = TestClient(bot_app)
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.main"):
        client.post("/telegram/webhook", json=payload)
    routed = _find(caplog.records, "telegram_update_routed")
    assert routed
    extra = routed[-1].__dict__
    assert extra["route"] == "kb_command"


def test_observability_json_serialisable_envelope(isolated_bot, caplog):
    """The extra dict must be JSON-serialisable so structured log shippers
    don't drop the line."""
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "chat": {"id": 100},
            "from": {"id": 200, "username": "ajdevy"},
            "text": "ping",
        },
    }
    client = TestClient(bot_app)
    with caplog.at_level(logging.INFO, logger="services.bot_gateway.app.main"):
        client.post("/telegram/webhook", json=payload)
    records = _find(caplog.records, "telegram_update_received")
    assert records
    envelope = {
        key: value
        for key, value in records[0].__dict__.items()
        if key
        in {
            "update_id",
            "chat_id",
            "username",
            "has_text",
            "caption_present",
            "media_group_id",
            "attachment_count",
            "attachment_kinds",
            "attachment_sizes",
        }
    }
    # Must round-trip through json without raising.
    json.dumps(envelope)
