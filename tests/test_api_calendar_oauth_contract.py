from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from services.api.app import main as api_main
from services.api.app.calendar.oauth import CalendarOAuthClient, OAuthExchangeError
from services.api.app.calendar.oauth_state_repository import (
    CalendarOAuthStateRepository,
)
from services.api.app.calendar.settings_repository import CalendarSettingsRepository
from services.api.app.calendar.token_repository import (
    CalendarTokenRepository,
    TokenNotFound,
)
from services.api.app.main import app as api_app

_INTERNAL_TOKEN = "test-internal-token"
_AUTH = {"Authorization": f"Bearer {_INTERNAL_TOKEN}"}
_PROJECT_ID = 7
_OPERATOR = "@op"


@pytest.fixture(autouse=True)
def _reset_rate_limit() -> Iterator[None]:
    api_main._calendar_oauth_hits.clear()
    yield
    api_main._calendar_oauth_hits.clear()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
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
    monkeypatch.setattr(api_main.settings, "calendar_oauth_state_ttl_seconds", 300)
    monkeypatch.setattr(api_main, "calendar_settings_repository", settings_repo)
    monkeypatch.setattr(api_main, "calendar_oauth_state_repository", state_repo)
    monkeypatch.setattr(api_main, "calendar_token_repository", token_repo)
    monkeypatch.setattr(api_main, "calendar_oauth_client", oauth_client)

    client = TestClient(api_app)
    yield {
        "client": client,
        "settings_repo": settings_repo,
        "state_repo": state_repo,
        "token_repo": token_repo,
        "oauth_client": oauth_client,
    }


def _enable_project(settings_repo: CalendarSettingsRepository, operator: str) -> None:
    settings_repo.enable(_PROJECT_ID, calendar_operator=operator)


def _stub_exchange(monkeypatch, *, refresh_token: str = "refresh-1") -> None:
    flow = Mock()
    flow.fetch_token = Mock()
    flow.credentials = SimpleNamespace(
        refresh_token=refresh_token, token="access", expiry=None
    )
    monkeypatch.setattr(
        "services.api.app.calendar.oauth.Flow.from_client_config",
        lambda config, scopes: flow,
    )


# --- initiate -------------------------------------------------------------


def test_initiate_requires_internal_token(env):
    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
    )
    assert resp.status_code == 401


def test_initiate_returns_consent_url(env):
    _enable_project(env["settings_repo"], _OPERATOR)
    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    url = resp.json()["consent_url"]
    assert "calendar.readonly" in url
    assert "state=" in url
    # No token / code echoed.
    assert "refresh" not in resp.text.lower()


def test_initiate_400_when_project_disabled(env):
    """Documented removal: under the new "connect = enable" contract, the
    callback is the authoritative gate. /initiate must succeed on a fresh
    project so the operator can bootstrap; gating on enabled=1 here would
    make first-time consent impossible. See PR #76 follow-up."""
    # No settings row at all — brand-new project.
    assert env["settings_repo"].get(_PROJECT_ID) is None
    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert "consent_url" in resp.json()


def test_initiate_400_when_wrong_operator(env):
    """Documented removal: /initiate must succeed even when a *different*
    operator currently holds the designated-calendar-operator slot, so that
    operator handover / re-consent flows can run. The callback updates the
    designated operator on success."""
    _enable_project(env["settings_repo"], "@someone-else")
    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert "consent_url" in resp.json()


def test_initiate_succeeds_on_fresh_project_with_state_and_oauth_params(env):
    """Brand-new project (no calendar_project_settings row): /initiate
    returns 200 with a consent URL that contains the minted state, the
    configured client_id, and the configured redirect_uri."""
    assert env["settings_repo"].get(_PROJECT_ID) is None

    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    consent_url = resp.json()["consent_url"]

    # The minted state, client_id, and redirect_uri all appear in the URL.
    assert "state=" in consent_url
    assert "client_id=cid" in consent_url
    # redirect_uri is URL-encoded in the query string.
    assert "redirect_uri=https%3A%2F%2Fexample.test%2Fcalendar%2Foauth%2Fcallback" in consent_url


def test_initiate_succeeds_for_different_operator_than_designated(env):
    """Operator handover / re-consent: a different operator than the one
    currently designated can still initiate. The callback updates the
    designated operator on success."""
    _enable_project(env["settings_repo"], "@previous-operator")

    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    consent_url = resp.json()["consent_url"]
    assert "state=" in consent_url
    assert "client_id=cid" in consent_url


def test_initiate_503_when_oauth_not_configured(env, monkeypatch):
    monkeypatch.setattr(api_main, "calendar_oauth_client", None)
    resp = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 503


def test_initiate_rate_limited(env):
    _enable_project(env["settings_repo"], _OPERATOR)
    for _ in range(api_main._CALENDAR_OAUTH_RATE_LIMIT):
        ok = env["client"].post(
            "/calendar/connect/initiate",
            json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
            headers=_AUTH,
        )
        assert ok.status_code == 200
    limited = env["client"].post(
        "/calendar/connect/initiate",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert limited.status_code == 429


# --- callback -------------------------------------------------------------


def _mint_state(env) -> str:
    return env["state_repo"].create(
        project_id=_PROJECT_ID,
        operator=_OPERATOR,
        ttl_seconds=300,
        now=datetime.now(UTC),
    )


def test_callback_happy_stores_encrypted_token_and_success_html(env, monkeypatch):
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)
    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "auth-code"}
    )
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "подключ" in resp.text.lower()
    assert "refresh-secret" not in resp.text
    assert "auth-code" not in resp.text
    # Stored encrypted: round-trips through the repo, ciphertext on disk.
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"


# --- auto-enable on callback (no separate /enable endpoint) ----------------


def test_callback_auto_enables_fresh_project_with_defaults(env, monkeypatch):
    """A successful OAuth callback on a NOT-YET-ENABLED project flips it to
    enabled and records the connecting operator as the designated calendar
    operator, atomic with the token upsert. Defaults to project_timezone +
    lookahead from the settings repo defaults."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)
    # No prior settings row → "fresh project".
    assert env["settings_repo"].get(_PROJECT_ID) is None

    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert resp.status_code == 200

    assert env["settings_repo"].is_enabled(_PROJECT_ID) is True
    stored = env["settings_repo"].get(_PROJECT_ID)
    assert stored.calendar_operator == _OPERATOR
    # Repository defaults preserved on fresh insert.
    assert stored.project_timezone == "Europe/Moscow"
    assert stored.lookahead_days == 60


def test_callback_preserves_existing_settings_on_already_enabled(env, monkeypatch):
    """If the project is already enabled, the callback preserves the existing
    project_timezone / lookahead_days and only updates the designated operator
    (the new one is the connecting operator)."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    env["settings_repo"].enable(
        _PROJECT_ID,
        calendar_operator="@old_op",
        project_timezone="Asia/Yekaterinburg",
        lookahead_days=14,
    )
    state = _mint_state(env)
    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert resp.status_code == 200

    stored = env["settings_repo"].get(_PROJECT_ID)
    assert stored.enabled is True
    # Designated operator is the connecting one now.
    assert stored.calendar_operator == _OPERATOR
    # Existing tunables preserved (not overwritten with defaults).
    assert stored.project_timezone == "Asia/Yekaterinburg"
    assert stored.lookahead_days == 14


def test_callback_enable_failure_returns_500_and_logs(env, monkeypatch, caplog):
    """If `settings_repo.enable` raises after `token_repo.upsert` succeeds, the
    callback returns a 500-class error rather than rendering a misleading
    success page, and the failure is logged with the operator+project context.
    The token remains stored (caller can retry by re-running /connect_calendar)."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    real_enable = env["settings_repo"].enable

    def boom(project_id, **kwargs):
        raise RuntimeError("simulated enable failure")

    monkeypatch.setattr(env["settings_repo"], "enable", boom)

    with caplog.at_level("ERROR", logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 500
    assert "text/html" in resp.headers["content-type"]
    assert "ошибка" in resp.text.lower()
    # Failure logged for the operator/project.
    assert any(
        "calendar_oauth_callback_enable_failed" in record.message
        for record in caplog.records
    )

    # Token was upserted before the enable failure; restoring the real enable
    # lets the operator retry (no manual cleanup needed).
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"
    monkeypatch.setattr(env["settings_repo"], "enable", real_enable)


def test_callback_400_html_on_unknown_state(env):
    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": "forged", "code": "c"}
    )
    assert resp.status_code == 400
    assert "text/html" in resp.headers["content-type"]
    assert "ошибка" in resp.text.lower()


def test_callback_400_html_on_replayed_state(env, monkeypatch):
    _stub_exchange(monkeypatch)
    state = _mint_state(env)
    first = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert first.status_code == 200
    replay = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert replay.status_code == 400


def test_callback_400_html_on_expired_state(env):
    # Mint a state in the past with a tiny TTL so it is already expired when
    # the callback consumes it with the real clock.
    state = env["state_repo"].create(
        project_id=_PROJECT_ID,
        operator=_OPERATOR,
        ttl_seconds=1,
        now=datetime.now(UTC) - timedelta(hours=1),
    )
    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert resp.status_code == 400


def test_callback_400_when_missing_params(env):
    resp = env["client"].get("/calendar/oauth/callback")
    assert resp.status_code == 400


def test_callback_400_on_exchange_failure_stores_nothing(env, monkeypatch):
    state = _mint_state(env)
    monkeypatch.setattr(
        env["oauth_client"],
        "exchange_code",
        Mock(side_effect=OAuthExchangeError("exchange_failed")),
    )
    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "bad"}
    )
    assert resp.status_code == 400
    with pytest.raises(TokenNotFound):
        env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR)


def test_callback_503_when_not_configured(env, monkeypatch):
    monkeypatch.setattr(api_main, "calendar_token_repository", None)
    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": "s", "code": "c"}
    )
    assert resp.status_code == 503


def test_callback_rate_limited(env, monkeypatch):
    _stub_exchange(monkeypatch)
    for _ in range(api_main._CALENDAR_OAUTH_RATE_LIMIT):
        env["client"].get(
            "/calendar/oauth/callback", params={"state": "x", "code": "c"}
        )
    limited = env["client"].get(
        "/calendar/oauth/callback", params={"state": "x", "code": "c"}
    )
    assert limited.status_code == 429


# --- FR-18 R2: Telegram confirmation DM on successful connect ------------


def test_callback_success_dms_operator(env, monkeypatch):
    """Happy path: a successful OAuth callback DMs the operator a Russian
    confirmation via telegram_bot_sender. The DM is sent to the chat_id
    resolved from the operator registry (NOT the fallback)."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    # Operator registry resolves the chat_id for our connecting operator.
    record = SimpleNamespace(chat_id=12345)
    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=record),
    )

    send_message = AsyncMock()
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert resp.status_code == 200
    # Token row IS saved (DM is best-effort, OAuth handshake succeeded).
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"

    send_message.assert_awaited_once()
    kwargs = send_message.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert "Календарь подключён" in kwargs["text"]


def test_callback_success_skips_dm_when_already_connected(env, monkeypatch, caplog):
    """Regression: a successful re-consent callback for an operator who was
    ALREADY connected to this project does NOT DM. Prevents spam when the
    operator (or their browser) replays the OAuth flow across api restarts.
    The token row IS still updated with the new refresh_token, and a
    `calendar_connect_dm_skipped_already_connected` log line is emitted."""
    # Pre-populate the token so the (project, operator) is already connected
    # BEFORE this callback runs.
    env["token_repo"].upsert(_PROJECT_ID, _OPERATOR, "old-refresh-secret")

    _stub_exchange(monkeypatch, refresh_token="new-refresh-secret")
    state = _mint_state(env)

    record = SimpleNamespace(chat_id=12345)
    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=record),
    )

    send_message = AsyncMock()
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    with caplog.at_level(logging.INFO, logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 200
    # The DM must NOT fire on re-consent.
    send_message.assert_not_awaited()
    # The new refresh_token IS persisted (upsert always runs).
    assert (
        env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR)
        == "new-refresh-secret"
    )
    # The skip is observable via the structured log.
    assert any(
        "calendar_connect_dm_skipped_already_connected" in record.message
        for record in caplog.records
    )


def test_callback_success_skips_dm_when_no_chat_id(env, monkeypatch, caplog):
    """Operator NOT in registry AND effective operator chat_id returns None
    → send_message is NOT called; calendar_connect_dm_no_chat_id is logged;
    HTML success is still 200; token row IS saved."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=None),
    )
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: None)

    send_message = AsyncMock()
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    with caplog.at_level(logging.INFO, logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 200
    send_message.assert_not_awaited()
    assert any(
        "calendar_connect_dm_no_chat_id" in record.message for record in caplog.records
    )
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"


@pytest.mark.parametrize(
    "exc",
    [
        httpx.RequestError("boom"),
        httpx.HTTPStatusError(
            "bad",
            request=httpx.Request("POST", "https://api.telegram.org"),
            response=httpx.Response(500),
        ),
    ],
)
def test_callback_success_swallows_dm_send_failure(env, monkeypatch, caplog, exc):
    """If telegram_bot_sender.send_message raises a transport / HTTP error,
    the callback still returns 200 + HTML success; the failure is logged via
    calendar_connect_dm_failed; the token row IS saved."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    record = SimpleNamespace(chat_id=77)
    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=record),
    )

    send_message = AsyncMock(side_effect=exc)
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    with caplog.at_level(logging.WARNING, logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 200
    send_message.assert_awaited_once()
    assert any(
        "calendar_connect_dm_failed" in r.message for r in caplog.records
    )
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"


def test_callback_success_uses_fallback_chat_id_when_operator_not_in_registry(
    env, monkeypatch
):
    """find_by_username returns None → fall back to _effective_hitl_operator_chat_id
    (string value parsed as int)."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=None),
    )
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: "4242")

    send_message = AsyncMock()
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    resp = env["client"].get(
        "/calendar/oauth/callback", params={"state": state, "code": "c"}
    )
    assert resp.status_code == 200
    send_message.assert_awaited_once()
    assert send_message.await_args.kwargs["chat_id"] == 4242


def test_callback_success_swallows_operator_lookup_failure(env, monkeypatch, caplog):
    """If operator_repository.find_by_username raises, we treat it as
    "no record" and fall through to the fallback (or skip DM) — never
    propagate. With no fallback available, the no_chat_id log fires; HTML success
    still 200; token saved."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(side_effect=RuntimeError("registry down")),
    )
    monkeypatch.setattr(api_main, "_effective_hitl_operator_chat_id", lambda: None)

    send_message = AsyncMock()
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    with caplog.at_level(logging.INFO, logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 200
    send_message.assert_not_awaited()
    assert any(
        "calendar_connect_dm_no_chat_id" in r.message for r in caplog.records
    )
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"


def test_callback_success_skips_dm_when_fallback_chat_id_is_unparseable(
    env, monkeypatch, caplog
):
    """Misconfigured non-numeric fallback chat_id is treated as missing — no
    DM, log line fires, HTML success still 200, token saved."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=None),
    )
    monkeypatch.setattr(
        api_main, "_effective_hitl_operator_chat_id", lambda: "not-an-int"
    )

    send_message = AsyncMock()
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    with caplog.at_level(logging.INFO, logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 200
    send_message.assert_not_awaited()
    assert any(
        "calendar_connect_dm_no_chat_id" in r.message for r in caplog.records
    )
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"


def test_callback_success_swallows_unexpected_dm_exception(env, monkeypatch, caplog):
    """Defensive: an unexpected (non-httpx) exception from send_message is
    also swallowed → HTML success 200, calendar_connect_dm_failed logged,
    token saved. Guarantees the OAuth handshake is never broken by a bot
    transport hiccup."""
    _stub_exchange(monkeypatch, refresh_token="refresh-secret")
    state = _mint_state(env)

    record = SimpleNamespace(chat_id=99)
    monkeypatch.setattr(
        api_main.operator_repository,
        "find_by_username",
        Mock(return_value=record),
    )
    send_message = AsyncMock(side_effect=RuntimeError("bot crashed"))
    monkeypatch.setattr(api_main.telegram_bot_sender, "send_message", send_message)

    with caplog.at_level(logging.WARNING, logger="services.api.app.main"):
        resp = env["client"].get(
            "/calendar/oauth/callback", params={"state": state, "code": "c"}
        )

    assert resp.status_code == 200
    send_message.assert_awaited_once()
    assert any(
        "calendar_connect_dm_failed" in r.message for r in caplog.records
    )
    assert env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR) == "refresh-secret"


# --- disconnect -----------------------------------------------------------


def test_disconnect_requires_internal_token(env):
    resp = env["client"].post(
        "/calendar/disconnect",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
    )
    assert resp.status_code == 401


def test_disconnect_revokes_and_deletes(env, monkeypatch):
    env["token_repo"].upsert(_PROJECT_ID, _OPERATOR, "refresh-secret")
    revoke = AsyncMock()
    monkeypatch.setattr(env["oauth_client"], "revoke", revoke)
    resp = env["client"].post(
        "/calendar/disconnect",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {"disconnected": True}
    revoke.assert_awaited_once_with(refresh_token="refresh-secret")
    with pytest.raises(TokenNotFound):
        env["token_repo"].get_refresh_token(_PROJECT_ID, _OPERATOR)


def test_disconnect_when_no_token_skips_revoke(env, monkeypatch):
    revoke = AsyncMock()
    monkeypatch.setattr(env["oauth_client"], "revoke", revoke)
    resp = env["client"].post(
        "/calendar/disconnect",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 200
    revoke.assert_not_called()


def test_disconnect_503_when_not_configured(env, monkeypatch):
    monkeypatch.setattr(api_main, "calendar_oauth_client", None)
    resp = env["client"].post(
        "/calendar/disconnect",
        json={"project_id": _PROJECT_ID, "operator": _OPERATOR},
        headers=_AUTH,
    )
    assert resp.status_code == 503
