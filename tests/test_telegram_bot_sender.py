import httpx
import pytest

from services.api.app import telegram_bot_sender as sender_module
from services.api.app.telegram_bot_sender import TelegramBotSender


@pytest.mark.asyncio
async def test_send_message_requires_token():
    sender = TelegramBotSender(bot_token="")
    with pytest.raises(RuntimeError, match="missing_bot_token"):
        await sender.send_message(chat_id=1, text="test")


@pytest.mark.asyncio
async def test_send_message_rejects_placeholder_token():
    sender = TelegramBotSender(bot_token="replace-me")
    with pytest.raises(RuntimeError, match="missing_bot_token"):
        await sender.send_message(chat_id=1, text="test")


class _CapturingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        self.calls.append((url, json))

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"ok": True, "result": {"message_id": 77}}

        return _Resp()


@pytest.mark.asyncio
async def test_send_message_uses_custom_base_url(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(
        bot_token="abc", base_url="http://local-bot-api:8081"
    )
    await sender.send_message(chat_id=10, text="hi")
    url = client.calls[0][0]
    assert url == "http://local-bot-api:8081/botabc/sendMessage"


@pytest.mark.asyncio
async def test_identity_methods_use_custom_base_url(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(
        bot_token="abc", base_url="http://local-bot-api:8081"
    )
    await sender.set_my_name(name="X")
    assert client.calls[0][0].startswith(
        "http://local-bot-api:8081/botabc/setMyName"
    )


@pytest.mark.asyncio
async def test_send_message_success(monkeypatch):
    captured: list[tuple[str, dict]] = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"message_id": 77}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: FakeClient())
    sender = TelegramBotSender(bot_token="token")
    assert await sender.send_message(chat_id=10, text="hello") == 77
    assert captured[0][0].endswith("/bot{0}/sendMessage".format("token"))
    assert captured[0][1] == {"chat_id": 10, "text": "hello"}


@pytest.mark.asyncio
async def test_send_message_includes_reply_markup(monkeypatch):
    captured: list[dict] = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"message_id": 88}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            captured.append(json)
            return FakeResponse()

    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: FakeClient())
    sender = TelegramBotSender(bot_token="token")
    markup = {"inline_keyboard": [[{"text": "ok", "callback_data": "x"}]]}
    assert (
        await sender.send_message(chat_id=10, text="hello", reply_markup=markup) == 88
    )
    assert captured[0]["reply_markup"] == markup


@pytest.mark.asyncio
async def test_set_my_name_posts_setMyName_payload(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="abc")
    result = await sender.set_my_name(name="Анна Иванова")
    assert result == {"ok": True, "result": {"message_id": 77}}
    assert client.calls[0][0].endswith("/botabc/setMyName")
    assert client.calls[0][1] == {"name": "Анна Иванова"}


@pytest.mark.asyncio
async def test_set_my_description_posts_setMyDescription_payload(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="abc")
    await sender.set_my_description(description="Здравствуйте!")
    assert client.calls[0][0].endswith("/botabc/setMyDescription")
    assert client.calls[0][1] == {"description": "Здравствуйте!"}


@pytest.mark.asyncio
async def test_set_my_short_description_posts_setMyShortDescription_payload(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="abc")
    await sender.set_my_short_description(short_description="На связи.")
    assert client.calls[0][0].endswith("/botabc/setMyShortDescription")
    assert client.calls[0][1] == {"short_description": "На связи."}


@pytest.mark.asyncio
async def test_answer_callback_query_posts_payload(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="abc")
    result = await sender.answer_callback_query(callback_query_id="cq-1", text="ok")
    assert result == {"ok": True, "result": {"message_id": 77}}
    assert client.calls[0][0].endswith("/botabc/answerCallbackQuery")
    assert client.calls[0][1] == {"callback_query_id": "cq-1", "text": "ok"}


@pytest.mark.asyncio
async def test_edit_message_reply_markup_posts_payload(monkeypatch):
    client = _CapturingClient()
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="abc")
    result = await sender.edit_message_reply_markup(
        chat_id=1,
        message_id=2,
        reply_markup=None,
    )
    assert result == {"ok": True, "result": {"message_id": 77}}
    assert client.calls[0][0].endswith("/botabc/editMessageReplyMarkup")
    assert client.calls[0][1] == {
        "chat_id": 1,
        "message_id": 2,
        "reply_markup": None,
    }


class _FlakyClient:
    """Async client double whose first .post() raises and second succeeds."""

    def __init__(self, *, first_error: Exception) -> None:
        self._first_error = first_error
        self.call_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, json):
        self.call_count += 1
        if self.call_count == 1:
            raise self._first_error

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"result": {"message_id": 77}}

        return _Resp()


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.telegram.org/")
    response = httpx.Response(status_code=status, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.mark.asyncio
async def test_send_message_does_not_retry_on_5xx(monkeypatch):
    """sendMessage is NOT idempotent and Telegram has no dedup key, so a 5xx
    (which may have been raised AFTER the message was already delivered) must
    not be retried — doing so double-posts to the customer."""
    client = _FlakyClient(first_error=_http_status_error(503))
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="token")
    with pytest.raises(httpx.HTTPStatusError):
        await sender.send_message(chat_id=10, text="hi")
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_send_message_retries_once_on_connect_error(monkeypatch):
    """A ConnectError means the connection was never established, so the
    request never reached Telegram — safe to retry without double-posting."""
    client = _FlakyClient(first_error=httpx.ConnectError("dns failed"))
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="token")
    assert await sender.send_message(chat_id=10, text="hi") == 77
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_send_message_retries_once_on_connect_timeout(monkeypatch):
    client = _FlakyClient(first_error=httpx.ConnectTimeout("connect timed out"))
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="token")
    assert await sender.send_message(chat_id=10, text="hi") == 77
    assert client.call_count == 2


@pytest.mark.asyncio
async def test_send_message_does_not_retry_on_read_timeout(monkeypatch):
    """A ReadTimeout happens after the request was sent — Telegram may have
    already delivered the message, so retrying would double-post."""
    client = _FlakyClient(first_error=httpx.ReadTimeout("read timed out"))
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="token")
    with pytest.raises(httpx.ReadTimeout):
        await sender.send_message(chat_id=10, text="hi")
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_send_message_does_not_retry_on_4xx(monkeypatch):
    """4xx (bad chat_id, blocked bot, content too long) won't change on
    retry — retrying would waste a request slot under rate limits."""
    client = _FlakyClient(first_error=_http_status_error(400))
    monkeypatch.setattr(sender_module.httpx, "AsyncClient", lambda timeout: client)
    sender = TelegramBotSender(bot_token="token")
    with pytest.raises(httpx.HTTPStatusError):
        await sender.send_message(chat_id=10, text="hi")
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_identity_methods_skip_when_token_unconfigured():
    sender_a = TelegramBotSender(bot_token="")
    sender_b = TelegramBotSender(bot_token="replace-me")
    for sender in (sender_a, sender_b):
        result = await sender.set_my_name(name="x")
        assert result == {"ok": False, "skipped": "missing_bot_token"}
        result = await sender.set_my_description(description="x")
        assert result == {"ok": False, "skipped": "missing_bot_token"}
        result = await sender.set_my_short_description(short_description="x")
        assert result == {"ok": False, "skipped": "missing_bot_token"}
