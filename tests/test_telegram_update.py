import pytest

from services.bot_gateway.app.telegram_update import (
    NormalizedTelegramMessage,
    TelegramUpdateValidationError,
    normalize_update,
)


def test_normalize_update_accepts_valid_text_message():
    payload = {
        "update_id": 10,
        "message": {
            "message_id": 22,
            "chat": {"id": 33},
            "from": {"id": 44},
            "text": "  hello  ",
        },
    }
    normalized = normalize_update(payload)
    assert normalized == NormalizedTelegramMessage(
        update_id=10,
        source_message_id=22,
        chat_id=33,
        user_id=44,
        username=None,
        text="hello",
    )


def test_normalize_update_extracts_username():
    payload = {
        "update_id": 11,
        "message": {
            "message_id": 23,
            "chat": {"id": 34},
            "from": {"id": 45, "username": "ajdevy"},
            "text": "set config",
        },
    }
    normalized = normalize_update(payload)
    assert normalized is not None
    assert normalized.username == "@ajdevy"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({}, "missing_or_invalid_update_id"),
        ({"update_id": "x"}, "missing_or_invalid_update_id"),
        ({"update_id": 1, "message": "bad"}, "invalid_message_object"),
        ({"update_id": 1, "message": {}}, "missing_or_invalid_message_id"),
        ({"update_id": 1, "message": {"message_id": 2}}, "missing_or_invalid_chat"),
        (
            {"update_id": 1, "message": {"message_id": 2, "chat": {"id": "x"}}},
            "missing_or_invalid_chat_id",
        ),
        (
            {"update_id": 1, "message": {"message_id": 2, "chat": {"id": 3}}},
            "missing_or_invalid_from",
        ),
        (
            {
                "update_id": 1,
                "message": {"message_id": 2, "chat": {"id": 3}, "from": {"id": "x"}},
            },
            "missing_or_invalid_user_id",
        ),
    ],
)
def test_normalize_update_validation_errors(payload, reason):
    with pytest.raises(TelegramUpdateValidationError, match=reason):
        normalize_update(payload)


def test_normalize_update_extracts_reply_to_caption() -> None:
    """Story 12.05: the ``/material`` handler reads ``reply_to_caption`` to
    fall back to the original media caption when the operator omits one."""
    payload = {
        "update_id": 99,
        "message": {
            "message_id": 2,
            "chat": {"id": 3},
            "from": {"id": 4, "username": "op"},
            "text": "/material",
            "reply_to_message": {
                "message_id": 1,
                "chat": {"id": 3},
                "from": {"id": 4, "username": "op"},
                "video": {
                    "file_id": "TG-V",
                    "file_size": 1,
                    "mime_type": "video/mp4",
                },
                "caption": "Тур-превью",
            },
        },
    }
    normalized = normalize_update(payload)
    assert normalized is not None
    assert normalized.reply_to_caption == "Тур-превью"


@pytest.mark.parametrize(
    "payload",
    [
        {"update_id": 1},
        {
            "update_id": 1,
            "message": {"message_id": 2, "chat": {"id": 3}, "from": {"id": 4}, "photo": []},
        },
        {
            "update_id": 1,
            "message": {"message_id": 2, "chat": {"id": 3}, "from": {"id": 4}, "text": "   "},
        },
    ],
)
def test_normalize_update_ignored_cases(payload):
    assert normalize_update(payload) is None
