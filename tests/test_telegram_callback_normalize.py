import pytest

from services.bot_gateway.app.telegram_callback import normalize_callback_query


def test_normalize_callback_query_happy_path_with_message():
    payload = {
        "update_id": 1,
        "callback_query": {
            "id": "cq-1",
            "from": {"id": 7, "username": "alex"},
            "data": "op_reg:approve:11",
            "message": {"message_id": 42, "chat": {"id": 99}},
        },
    }
    normalized = normalize_callback_query(payload)
    assert normalized is not None
    assert normalized.update_id == 1
    assert normalized.callback_query_id == "cq-1"
    assert normalized.chat_id == 99
    assert normalized.sender_username == "@alex"
    assert normalized.sender_user_id == 7
    assert normalized.data == "op_reg:approve:11"
    assert normalized.source_message_id == 42


def test_normalize_callback_query_uses_sender_id_when_message_absent():
    payload = {
        "update_id": 2,
        "callback_query": {
            "id": "cq-2",
            "from": {"id": 1234},
            "data": "onboard:tg:5",
        },
    }
    normalized = normalize_callback_query(payload)
    assert normalized is not None
    assert normalized.chat_id == 1234
    assert normalized.sender_username is None
    assert normalized.source_message_id is None


def test_normalize_callback_query_returns_none_for_non_callback():
    payload = {"update_id": 3, "message": {"message_id": 1}}
    assert normalize_callback_query(payload) is None


@pytest.mark.parametrize(
    "payload,reason",
    [
        ({}, "missing_or_invalid_update_id"),
        ({"update_id": 1, "callback_query": []}, "invalid_callback_query"),
        (
            {"update_id": 1, "callback_query": {"from": {"id": 1}, "data": "x"}},
            "missing_or_invalid_callback_query_id",
        ),
        ({"update_id": 1, "callback_query": {"id": "1", "data": "x"}}, "missing_or_invalid_from"),
        (
            {
                "update_id": 1,
                "callback_query": {"id": "1", "from": {"id": "bad"}, "data": "x"},
            },
            "missing_or_invalid_sender_user_id",
        ),
        (
            {
                "update_id": 1,
                "callback_query": {"id": "1", "from": {"id": 2}, "data": ""},
            },
            "missing_or_invalid_callback_data",
        ),
    ],
)
def test_normalize_callback_query_validation(payload, reason):
    with pytest.raises(ValueError, match=reason):
        normalize_callback_query(payload)
