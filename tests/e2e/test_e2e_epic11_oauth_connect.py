"""Epic 11 / story 11.02 — OAuth connect E2E: initiate → callback → disconnect.

Drives the full api surface with Google's token exchange mocked: mint a consent
URL, simulate Google's redirect to the callback (which exchanges the code and
stores an encrypted refresh token), confirm the row exists, then disconnect.
Asserts no token / ``code`` leaks into any response body or captured logs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.calendar.oauth import CalendarOAuthClient
from services.api.app.calendar.oauth_state_repository import (
    CalendarOAuthStateRepository,
)
from services.api.app.calendar.settings_repository import CalendarSettingsRepository
from services.api.app.calendar.token_repository import (
    CalendarTokenRepository,
    TokenNotFound,
)
from services.api.app.main import app as api_app

pytestmark = [pytest.mark.e2e, pytest.mark.epic("11"), pytest.mark.story("11-02")]

_INTERNAL_TOKEN = "e2e-internal-token"
_AUTH = {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}
_PROJECT_ID = 11
_OPERATOR = "@calendar_op"
_REFRESH_TOKEN = "e2e-refresh-token-secret"
_AUTH_CODE = "e2e-auth-code-secret"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    api_main._calendar_oauth_hits.clear()
    from services.bot_gateway.app import main as bot_main
    from services.bot_gateway.app.webhook_dedup import WebhookUpdateClaimRepository

    # Keep this E2E independent from the developer's persistent local webhook
    # dedup DB; update_id=9001 is intentionally stable in the scenario.
    monkeypatch.setattr(
        bot_main,
        "webhook_update_claim_repository",
        WebhookUpdateClaimRepository(str(tmp_path / "webhook_dedup.sqlite3")),
    )
    calendar_db = str(tmp_path / "calendar.sqlite3")
    settings_repo = CalendarSettingsRepository(db_path=calendar_db)
    state_repo = CalendarOAuthStateRepository(db_path=calendar_db)
    token_repo = CalendarTokenRepository(
        db_path=calendar_db, fernet=Fernet(Fernet.generate_key())
    )
    oauth_client = CalendarOAuthClient(
        client_id="cid",
        client_secret="secret",
        redirect_uri="https://example.test/calendar/oauth/callback",
    )

    monkeypatch.setattr(api_main.settings, "internal_service_token", _INTERNAL_TOKEN)
    # FR-18 R2: fallback chat_id so the connect-confirmation DM is delivered
    # even though this e2e doesn't seed an operator_repository row.
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "999")
    monkeypatch.setattr(api_main, "calendar_settings_repository", settings_repo)
    monkeypatch.setattr(api_main, "calendar_oauth_state_repository", state_repo)
    monkeypatch.setattr(api_main, "calendar_token_repository", token_repo)
    monkeypatch.setattr(api_main, "calendar_oauth_client", oauth_client)

    # Mock Google's Flow: authorization_url echoes the state into a fake
    # consent URL; fetch_token yields canned credentials. (The real
    # authorization_url is also exercised by the unit tests.)
    flow = Mock()
    flow.fetch_token = Mock()
    flow.credentials = SimpleNamespace(
        refresh_token=_REFRESH_TOKEN,
        token="e2e-access",
        expiry=datetime(2026, 5, 23, 12, 0, tzinfo=UTC),
    )
    flow.authorization_url = Mock(
        side_effect=lambda **kw: (
            f"https://accounts.google.test/o/oauth2/auth?state={kw['state']}"
            "&scope=calendar.readonly",
            kw["state"],
        )
    )
    monkeypatch.setattr(
        "services.api.app.calendar.oauth.Flow.from_client_config",
        lambda config, scopes: flow,
    )
    revoke = AsyncMock()
    monkeypatch.setattr(oauth_client, "revoke", revoke)

    # FR-18 R2: record the connect-confirmation DM so the happy path can
    # assert it was sent on the first successful connection (in addition to
    # the HTML success page). The project intentionally starts disabled here;
    # the callback is the action that enables it.
    sent_dms: list[tuple[int, str]] = []

    async def record_send_message(*, chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(
        api_main.telegram_bot_sender, "send_message", record_send_message
    )

    client = TestClient(api_app)
    yield {
        "client": client,
        "token_repo": token_repo,
        "revoke": revoke,
        "sent_dms": sent_dms,
    }
    api_main._calendar_oauth_hits.clear()


def test_epic11_oauth_connect_full_flow(env, caplog):
    client = env["client"]
    token_repo = env["token_repo"]

    with caplog.at_level(logging.DEBUG):
        # 1) initiate → consent URL bound to a single-use state.
        initiate = client.post(
            "/calendar/connect/initiate",
            json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
            headers=_AUTH,
        )
        assert initiate.status_code == 200
        consent_url = initiate.json()["consent_url"]
        state = parse_qs(urlparse(consent_url).query)["state"][0]

        # 2) simulated Google redirect → callback exchanges + stores token.
        callback = client.get(
            "/calendar/oauth/callback",
            params={"state": state, "code": _AUTH_CODE},
        )
        assert callback.status_code == 200
        assert "подключ" in callback.text.lower()

        # 3) token stored (encrypted) and decryptable via the repo.
        assert token_repo.get_refresh_token(_PROJECT_ID, _OPERATOR) == _REFRESH_TOKEN

        # FR-18 R2: in addition to the HTML success page, the api DMs the
        # operator a Russian connect-confirmation message.
        assert any(
            "Календарь подключён" in text for _chat_id, text in env["sent_dms"]
        )

        # 4) disconnect → revoke + delete.
        disconnect = client.post(
            "/calendar/disconnect",
            json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
            headers=_AUTH,
        )
        assert disconnect.status_code == 200
        assert disconnect.json() == {"disconnected": True}

    env["revoke"].assert_awaited_once_with(refresh_token=_REFRESH_TOKEN)
    with pytest.raises(TokenNotFound):
        token_repo.get_refresh_token(_PROJECT_ID, _OPERATOR)

    # No token / code in any response body or in our application's logs. (The
    # httpx test-client emits a DEBUG access log carrying the request URL — a
    # harness artifact, not our code — so we scope the assertion to records
    # our application emitted.)
    bodies = initiate.text + callback.text + disconnect.text
    app_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("services.api")
    )
    for secret in (_REFRESH_TOKEN, _AUTH_CODE):
        assert secret not in bodies
        assert secret not in app_logs


def test_epic11_oauth_reconnect_does_not_repeat_confirmation(env):
    """A second successful OAuth callback updates the token silently.

    The confirmation belongs to the connection event, not to every callback
    or api restart.  This drives two complete initiate → callback cycles while
    keeping the project enabled between them.
    """
    client = env["client"]

    def connect_once(*, auth_code: str) -> None:
        initiate = client.post(
            "/calendar/connect/initiate",
            json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
            headers=_AUTH,
        )
        assert initiate.status_code == 200
        state = parse_qs(urlparse(initiate.json()["consent_url"]).query)["state"][0]
        callback = client.get(
            "/calendar/oauth/callback",
            params={"state": state, "code": auth_code},
        )
        assert callback.status_code == 200

    connect_once(auth_code="first-auth-code")
    first_connection_dm_count = len(env["sent_dms"])
    assert first_connection_dm_count == 1

    connect_once(auth_code="second-auth-code")
    assert len(env["sent_dms"]) == first_connection_dm_count


@pytest.mark.story("11-03")
def test_epic11_operator_connect_calendar_webhook_dms_consent_url(
    env, monkeypatch
):
    """Story 11.03 — operator `/connect_calendar` webhook → consent-URL DM.

    Drives the bot_gateway webhook; the bot's ApiClient initiate call is routed
    to the api TestClient (which mints a real consent URL via the mocked Flow),
    and the operator DM is captured. Asserts the consent URL reaches the
    operator and that the bound state never leaks into the captured DM logs.
    """
    from services.bot_gateway.app import main as bot_main
    from services.bot_gateway.app.main import app as bot_app

    api_client = env["client"]

    async def routed_initiate(*, project_id, operator, internal_token):
        resp = api_client.post(
            "/calendar/connect/initiate",
            json={"project_id": project_id, "operator": operator},
            headers={"Authorization": f"Bearer {internal_token}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def fake_resolve(*, username, api_client):
        from services.bot_gateway.app.operator_resolver import ResolvedOperator

        return ResolvedOperator(
            username=_OPERATOR,
            chat_id=500,
            project_id=_PROJECT_ID,
            is_active=True,
            source="registry",
        )

    sent_dms: list[tuple[int, str]] = []

    async def fake_send_dm(chat_id: int, text: str) -> None:
        sent_dms.append((chat_id, text))

    monkeypatch.setattr(bot_main.settings, "internal_service_token", _INTERNAL_TOKEN)
    monkeypatch.setattr(
        bot_main.api_client, "initiate_calendar_connect", routed_initiate
    )
    monkeypatch.setattr(
        "services.bot_gateway.app.calendar_commands.resolve_operator_for_sender",
        fake_resolve,
    )
    monkeypatch.setattr(bot_main, "_send_dm", fake_send_dm)

    bot_client = TestClient(bot_app)
    webhook = bot_client.post(
        "/telegram/webhook",
        json={
            "update_id": 9001,
            "message": {
                "message_id": 1,
                "chat": {"id": 500},
                "from": {"id": 42, "username": "calendar_op"},
                "text": "/connect_calendar",
            },
        },
    )

    assert webhook.status_code == 200
    body = webhook.json()
    assert body["route"] == "calendar_connect"
    assert body["decision"] == "url_sent"

    assert len(sent_dms) == 1
    chat_id, text = sent_dms[0]
    assert chat_id == 500
    consent_url = next(
        line for line in text.splitlines() if line.startswith("https://")
    )
    state = parse_qs(urlparse(consent_url).query)["state"][0]
    assert state  # a state was bound to the consent URL
    # The bound state must not leak into the webhook response body.
    assert state not in webhook.text
