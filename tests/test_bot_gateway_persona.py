from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from platform_common.settings import get_settings
from services.bot_gateway.app.main import (
    api_client,
    hitl_ticket_repository,
    telegram_bot_sender,
)
from services.bot_gateway.app.main import app as bot_app

_BOT_GATEWAY_LOGGER = "services.bot_gateway.app.main"

_PERSONA_PROMPT_FIXTURE = (
    "📝 Как нас будут звать? Ответьте на это сообщение в формате «Имя» "
    "или «Имя Фамилия»."
)


@pytest.fixture(autouse=True)
def _isolated_bot_gateway(tmp_path, monkeypatch):
    hitl_ticket_repository.db_path = str(tmp_path / "hitl.sqlite3")
    persistence_path = tmp_path / "persistence.sqlite3"
    monkeypatch.setenv("PERSISTENCE_DB_PATH", str(persistence_path))
    get_settings.cache_clear()

    async def _default_operator_lookup(*, username: str):
        if username == "@ajdevy":
            return {"username": "@ajdevy", "chat_id": 1, "project_id": 1, "is_active": True}
        return None

    monkeypatch.setattr(api_client, "find_operator_by_username", _default_operator_lookup)
    yield
    get_settings.cache_clear()


def _operator_payload(
    *,
    text: str,
    reply_to_text: str | None = None,
    username: str = "ajdevy",
    update_id: int = 5001,
) -> dict:
    msg: dict = {
        "message_id": 1,
        "from": {"id": 1, "username": username},
        "chat": {"id": 1, "type": "private"},
        "text": text,
    }
    if reply_to_text is not None:
        msg["reply_to_message"] = {"text": reply_to_text}
    return {"update_id": update_id, "message": msg}


def test_persona_command_oneshot_calls_api_and_sends_confirmation(monkeypatch):
    set_persona = AsyncMock(
        return_value={"first_name": "Мария", "last_name": "Петрова", "full_name": "Мария Петрова"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Мария Петрова"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "persona_updated"
    assert body["first_name"] == "Мария"
    assert body["last_name"] == "Петрова"
    set_persona.assert_awaited_once_with(
        first_name="Мария", last_name="Петрова", updated_by="@ajdevy"
    )
    send.assert_awaited_once()
    assert "Мария Петрова" in send.await_args.kwargs["text"]


def test_persona_command_without_args_sends_dialog_prompt(monkeypatch):
    set_persona = AsyncMock()
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "persona_prompt_sent"
    set_persona.assert_not_awaited()
    send.assert_awaited_once()
    assert "Как нас будут звать?" in send.await_args.kwargs["text"]


def test_persona_natural_trigger_without_name_sends_dialog_prompt(monkeypatch):
    """Bare natural triggers (no name in the same line) still open the full dialog."""
    set_persona = AsyncMock()
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    for trigger in ("смени имя", "Поменяй имя", "переименуй", "новое имя"):
        send.reset_mock()
        response = client.post(
            "/telegram/webhook",
            json=_operator_payload(text=trigger, update_id=hash(trigger) & 0x7FFFFFFF),
        )
        assert response.json()["status"] == "persona_prompt_sent", trigger
        send.assert_awaited_once()
        assert "Как нас будут звать?" in send.await_args.kwargs["text"]
    set_persona.assert_not_awaited()


def test_persona_reply_to_prompt_applies_new_persona(monkeypatch):
    set_persona = AsyncMock(
        return_value={"first_name": "Иван", "last_name": "Сидоров"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="Иван Сидоров",
            reply_to_text=_PERSONA_PROMPT_FIXTURE,
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="Иван", last_name="Сидоров", updated_by="@ajdevy"
    )


def test_persona_reply_with_only_first_name_applies(monkeypatch):
    """Reply to the full prompt with a single token applies that as the first
    name with an empty surname — the surname is optional."""
    set_persona = AsyncMock(
        return_value={"first_name": "ТолькоИмя", "last_name": ""}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="ТолькоИмя",
            reply_to_text=_PERSONA_PROMPT_FIXTURE,
        ),
    )
    assert response.json()["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="ТолькоИмя", last_name="", updated_by="@ajdevy"
    )
    send.assert_awaited_once()
    # Confirmation has no trailing space when last_name is empty.
    assert send.await_args.kwargs["text"] == "Готово, теперь меня зовут ТолькоИмя."


def test_persona_caller_other_than_effective_operator_replies_with_diagnostic(monkeypatch):
    """A non-operator user trying to rename the bot is rejected with a visible
    reply that names both the configured operator and the sender's username,
    so they can self-diagnose username/operator mismatches without spelunking
    through container logs (the original 'ничего не происходит' bug)."""
    set_persona = AsyncMock()
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова", username="random_user"),
    )
    assert response.json()["status"] == "ignored"
    assert response.json()["reason"] == "unauthorized_persona"
    set_persona.assert_not_awaited()
    send.assert_awaited_once()
    sent_text = send.await_args.kwargs["text"]
    assert "Сменить имя бота может только" in sent_text
    assert "@random_user" in sent_text  # sender as the bot saw them


def test_persona_runtime_configured_operator_can_rename(monkeypatch):
    """A registered operator (not the default admin) is authorized to rename
    the bot — regression fix for 'смени имя на …' silently failing for
    non-admin operators when they were registered in the operators registry."""
    monkeypatch.setattr(
        api_client,
        "find_operator_by_username",
        AsyncMock(
            return_value={
                "username": "@support_b", "chat_id": 1, "project_id": 1, "is_active": True
            }
        ),
    )
    set_persona = AsyncMock(
        return_value={"first_name": "Анна", "last_name": "Иванова"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова", username="support_b"),
    )
    assert response.json()["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="Анна", last_name="Иванова", updated_by="@support_b"
    )


def test_persona_unregistered_sender_is_rejected(monkeypatch):
    """A sender not in the operators registry is rejected from persona commands."""
    monkeypatch.setattr(
        api_client,
        "find_operator_by_username",
        AsyncMock(return_value=None),
    )
    set_persona = AsyncMock()
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова", username="ajdevy"),
    )
    assert response.json()["reason"] == "unauthorized_persona"
    set_persona.assert_not_awaited()
    send.assert_awaited_once()
    sent_text = send.await_args.kwargs["text"]
    assert "@ajdevy" in sent_text


def test_persona_reply_from_non_operator_is_rejected_with_diagnostic(monkeypatch):
    set_persona = AsyncMock()
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="Анна Иванова",
            reply_to_text="📝 Как нас будут звать?",
            username="random_user",
        ),
    )
    assert response.json()["reason"] == "unauthorized_persona"
    set_persona.assert_not_awaited()
    send.assert_awaited_once()
    assert "@random_user" in send.await_args.kwargs["text"]


def test_persona_api_failure_reports_status_to_operator(monkeypatch):
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(side_effect=RuntimeError("api down")),
    )
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова"),
    )
    assert response.json()["status"] == "persona_update_failed"
    send.assert_awaited_once()
    assert "не получилось" in send.await_args.kwargs["text"].lower()


def test_persona_slash_with_only_first_name_applies_immediately(monkeypatch):
    """/persona Анна → apply with empty surname; the surname is optional, so
    no follow-up dialog and no second user-visible message."""
    set_persona = AsyncMock(return_value={"first_name": "Анна", "last_name": ""})
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна"),
    )
    body = response.json()
    assert body["status"] == "persona_updated"
    assert body["first_name"] == "Анна"
    assert body["last_name"] == ""
    set_persona.assert_awaited_once_with(
        first_name="Анна", last_name="", updated_by="@ajdevy"
    )
    send.assert_awaited_once()
    assert send.await_args.kwargs["text"] == "Готово, теперь меня зовут Анна."


def test_persona_send_swallows_missing_bot_token(monkeypatch):
    """If telegram_bot_sender.send_message raises (no token), webhook still succeeds."""
    set_persona = AsyncMock(return_value={"first_name": "Анна", "last_name": "Иванова"})
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    monkeypatch.setattr(
        telegram_bot_sender,
        "send_message",
        AsyncMock(side_effect=RuntimeError("missing_bot_token")),
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "persona_updated"


def test_persona_trigger_does_not_hijack_unrelated_operator_reply(monkeypatch):
    """An operator reply that doesn't start with a persona trigger and isn't
    a reply to the persona marker must NOT enter the persona branch."""
    set_persona = AsyncMock()
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    deliver = AsyncMock(return_value={"delivered": True})
    monkeypatch.setattr(api_client, "deliver_operator_reply", deliver)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock())

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="ответ оператора",
            reply_to_text="HITL ticket #5 | from @c | q",
        ),
    )
    assert response.json()["status"] == "operator_reply_delivered"
    set_persona.assert_not_awaited()


# --- One-shot natural trigger ------------------------------------------------


def test_persona_natural_trigger_with_first_and_last_applies_oneshot(monkeypatch):
    """`смени имя на Анна Иванова` must rename in one shot, no dialog."""
    set_persona = AsyncMock(
        return_value={"first_name": "Анна", "last_name": "Иванова"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="смени имя на Анна Иванова"),
    )
    assert response.json()["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="Анна", last_name="Иванова", updated_by="@ajdevy"
    )
    send.assert_awaited_once()
    assert "Анна Иванова" in send.await_args.kwargs["text"]


def test_persona_natural_trigger_with_only_first_applies_immediately(monkeypatch):
    """`смени имя на Анна` → apply with empty surname; one Telegram reply
    only, never the partial-dialog prompt that previously fired twice when the
    operator sent the trigger more than once."""
    set_persona = AsyncMock(return_value={"first_name": "Анна", "last_name": ""})
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="смени имя на Анна"),
    )
    body = response.json()
    assert body["status"] == "persona_updated"
    assert body["first_name"] == "Анна"
    assert body["last_name"] == ""
    set_persona.assert_awaited_once_with(
        first_name="Анна", last_name="", updated_by="@ajdevy"
    )
    send.assert_awaited_once()
    assert send.await_args.kwargs["text"] == "Готово, теперь меня зовут Анна."


def test_persona_natural_trigger_without_preposition_applies_oneshot(monkeypatch):
    """`новое имя Анна Иванова` (no «на» / «в») also one-shot renames."""
    set_persona = AsyncMock(
        return_value={"first_name": "Анна", "last_name": "Иванова"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="новое имя Анна Иванова"),
    )
    assert response.json()["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="Анна", last_name="Иванова", updated_by="@ajdevy"
    )


def test_persona_natural_trigger_with_inflected_preposition_v(monkeypatch):
    """`переименуй в Анну Иванову` (genitive after «в») applies as-is —
    declension is the operator's responsibility."""
    set_persona = AsyncMock(
        return_value={"first_name": "Анну", "last_name": "Иванову"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="переименуй в Анну Иванову"),
    )
    assert response.json()["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="Анну", last_name="Иванову", updated_by="@ajdevy"
    )


def test_persona_natural_trigger_extra_tokens_use_first_two(monkeypatch):
    """`смени имя на Анна Иванова Петрова` — extra tokens are ignored."""
    set_persona = AsyncMock(
        return_value={"first_name": "Анна", "last_name": "Иванова"}
    )
    monkeypatch.setattr(api_client, "set_persona", set_persona)
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=1))

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="смени имя на Анна Иванова Петрова"),
    )
    assert response.json()["status"] == "persona_updated"
    set_persona.assert_awaited_once_with(
        first_name="Анна", last_name="Иванова", updated_by="@ajdevy"
    )


# --- Observability: structured logs at every persona-handler outcome --------


def test_persona_unauthorized_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(api_client, "set_persona", AsyncMock())
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.WARNING, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова", username="random_user"),
    )

    records = [r for r in caplog.records if r.message == "persona_unauthorized"]
    assert len(records) == 1, [r.message for r in caplog.records]
    record = records[0]
    assert record.levelno == logging.WARNING
    assert record.username == "@random_user"


def test_persona_natural_oneshot_with_only_first_logs(monkeypatch, caplog):
    """`переименуй в Анна` (single token after the trigger) now applies in one
    shot — and emits the same `persona_natural_oneshot` event as the two-token
    case, with `last_name` empty."""
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(return_value={"first_name": "Анна", "last_name": ""}),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(text="переименуй в Анна"),
    )

    records = [r for r in caplog.records if r.message == "persona_natural_oneshot"]
    assert len(records) == 1
    assert records[0].first_name == "Анна"
    assert records[0].last_name == ""
    assert records[0].trigger == "переименуй"
    assert records[0].token_count == 1


def test_persona_slash_oneshot_with_only_first_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(return_value={"first_name": "Анна", "last_name": ""}),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_operator_payload(text="/persona Анна"))

    records = [r for r in caplog.records if r.message == "persona_slash_oneshot"]
    assert len(records) == 1
    assert records[0].first_name == "Анна"
    assert records[0].last_name == ""
    assert records[0].token_count == 1


def test_persona_slash_full_prompt_logs(monkeypatch, caplog):
    monkeypatch.setattr(api_client, "set_persona", AsyncMock())
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_operator_payload(text="/persona"))

    assert any(r.message == "persona_slash_full_prompt_sent" for r in caplog.records)


def test_persona_natural_full_prompt_logs_trigger(monkeypatch, caplog):
    monkeypatch.setattr(api_client, "set_persona", AsyncMock())
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post("/telegram/webhook", json=_operator_payload(text="смени имя"))

    records = [r for r in caplog.records if r.message == "persona_natural_full_prompt_sent"]
    assert len(records) == 1
    assert records[0].trigger == "смени имя"


def test_persona_slash_oneshot_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(return_value={"first_name": "Анна", "last_name": "Иванова"}),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова"),
    )

    assert any(r.message == "persona_slash_oneshot" for r in caplog.records)
    assert any(r.message == "persona_updated" for r in caplog.records)


def test_persona_natural_oneshot_logs_trigger(monkeypatch, caplog):
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(return_value={"first_name": "Анна", "last_name": "Иванова"}),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(text="смени имя на Анна Иванова"),
    )

    records = [r for r in caplog.records if r.message == "persona_natural_oneshot"]
    assert len(records) == 1
    assert records[0].trigger == "смени имя"


def test_persona_full_reply_accepted_logs(monkeypatch, caplog):
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(return_value={"first_name": "Иван", "last_name": "Сидоров"}),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="Иван Сидоров",
            reply_to_text=_PERSONA_PROMPT_FIXTURE,
        ),
    )

    assert any(r.message == "persona_full_reply_accepted" for r in caplog.records)


def test_persona_full_reply_with_two_tokens_logs(monkeypatch, caplog):
    """Reply to the full prompt with two tokens applies and logs the count."""
    monkeypatch.setattr(
        api_client,
        "set_persona",
        AsyncMock(return_value={"first_name": "Иван", "last_name": "Сидоров"}),
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.INFO, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(
            text="Иван Сидоров",
            reply_to_text=_PERSONA_PROMPT_FIXTURE,
        ),
    )

    records = [r for r in caplog.records if r.message == "persona_full_reply_accepted"]
    assert len(records) == 1
    assert records[0].first_name == "Иван"
    assert records[0].last_name == "Сидоров"
    assert records[0].token_count == 2


def test_persona_update_failed_logs_remain(monkeypatch, caplog):
    """The pre-existing `persona_update_failed` warning must still fire — these
    new logs are additive, not replacements."""
    monkeypatch.setattr(
        api_client, "set_persona", AsyncMock(side_effect=RuntimeError("api down"))
    )
    monkeypatch.setattr(telegram_bot_sender, "send_message", AsyncMock(return_value=42))
    caplog.set_level(logging.WARNING, logger=_BOT_GATEWAY_LOGGER)

    client = TestClient(bot_app)
    client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/persona Анна Иванова"),
    )

    assert any(r.message == "persona_update_failed" for r in caplog.records)


# --- /whoami diagnostic ------------------------------------------------------


def test_whoami_replies_with_match_when_sender_is_operator(monkeypatch):
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/whoami"),
    )

    assert response.json()["status"] == "whoami_sent"
    send.assert_awaited_once()
    sent_text = send.await_args.kwargs["text"]
    assert "@ajdevy" in sent_text  # username
    assert "✅" in sent_text  # match marker
    assert "1" in sent_text  # chat_id from _operator_payload (chat.id == 1)


def test_whoami_replies_with_mismatch_when_sender_is_not_operator(monkeypatch):
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/whoami", username="random_user"),
    )

    assert response.json()["status"] == "whoami_sent"
    send.assert_awaited_once()
    sent_text = send.await_args.kwargs["text"]
    assert "@random_user" in sent_text
    assert "❌" in sent_text


def test_whoami_handles_missing_username(monkeypatch):
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    payload = _operator_payload(text="/whoami")
    payload["message"]["from"].pop("username")

    client = TestClient(bot_app)
    response = client.post("/telegram/webhook", json=payload)

    assert response.json()["status"] == "whoami_sent"
    send.assert_awaited_once()
    sent_text = send.await_args.kwargs["text"]
    assert "(без username)" in sent_text
    assert "❌" in sent_text


def test_whoami_shows_platform_admin_for_admin_sender(monkeypatch):
    import services.bot_gateway.app.main as bot_main

    monkeypatch.setattr(bot_main.settings, "admin_telegram_username", "@ajdevy")
    monkeypatch.setattr(bot_main.settings, "hitl_config_admin_username", "@ajdevy")
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="/whoami"),
    )

    assert response.json()["status"] == "whoami_sent"
    sent_text = send.await_args.kwargs["text"]
    assert "👑" in sent_text
    assert "администратор платформы" in sent_text


def test_whoami_does_not_intercept_other_text(monkeypatch):
    """Make sure /whoami trigger only matches the slash command itself, not
    arbitrary text mentioning 'whoami'."""
    send = AsyncMock(return_value=42)
    monkeypatch.setattr(telegram_bot_sender, "send_message", send)
    monkeypatch.setattr(
        api_client,
        "forward_inbound",
        AsyncMock(return_value={"status": "ok"}),
    )

    client = TestClient(bot_app)
    response = client.post(
        "/telegram/webhook",
        json=_operator_payload(text="how do i whoami", username="random_user"),
    )

    body = response.json()
    assert body.get("status") != "whoami_sent"
