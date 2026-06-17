from services.api.app.calendar.oauth_production import (
    assess_calendar_oauth_production_readiness,
    expected_calendar_redirect_uri,
    host_from_web_ui_base_url,
    is_dev_redirect_uri,
)


def test_expected_calendar_redirect_uri() -> None:
    assert (
        expected_calendar_redirect_uri(host="semantaix.flexsentlabs.com")
        == "https://semantaix.flexsentlabs.com/api/calendar/oauth/callback"
    )


def test_host_from_web_ui_base_url() -> None:
    assert (
        host_from_web_ui_base_url("https://semantaix.flexsentlabs.com/admin")
        == "semantaix.flexsentlabs.com"
    )
    assert host_from_web_ui_base_url("http://localhost/admin") is None
    assert host_from_web_ui_base_url(None) is None
    assert host_from_web_ui_base_url("") is None


def test_is_dev_redirect_uri() -> None:
    assert is_dev_redirect_uri(None)
    assert is_dev_redirect_uri("")
    assert is_dev_redirect_uri(
        "https://lustiness-apron-unmade.ngrok-free.dev/api/calendar/oauth/callback"
    )
    assert not is_dev_redirect_uri(
        "https://semantaix.flexsentlabs.com/api/calendar/oauth/callback"
    )


def test_assess_non_production_always_ok() -> None:
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="development",
        redirect_uri="https://ngrok.test/callback",
        web_ui_base_url="http://localhost",
        oauth_configured=False,
    )
    assert ok is True
    assert detail["reason"] == "non_production"


def test_assess_production_prod_ready() -> None:
    redirect = "https://semantaix.flexsentlabs.com/api/calendar/oauth/callback"
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="production",
        redirect_uri=redirect,
        web_ui_base_url="https://semantaix.flexsentlabs.com/admin",
        oauth_configured=True,
    )
    assert ok is True
    assert detail["prod_ready"] is True
    assert detail["redirect_uri"] == redirect


def test_assess_production_rejects_ngrok() -> None:
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="production",
        redirect_uri="https://foo.ngrok-free.dev/api/calendar/oauth/callback",
        web_ui_base_url="https://semantaix.flexsentlabs.com/admin",
        oauth_configured=True,
    )
    assert ok is False
    assert detail["reason"] == "dev_redirect_in_production"


def test_assess_production_rejects_host_mismatch() -> None:
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="production",
        redirect_uri="https://other.example.com/api/calendar/oauth/callback",
        web_ui_base_url="https://semantaix.flexsentlabs.com/admin",
        oauth_configured=True,
    )
    assert ok is False
    assert detail["reason"] == "redirect_host_mismatch"


def test_assess_production_not_configured() -> None:
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="production",
        redirect_uri=None,
        web_ui_base_url="https://semantaix.flexsentlabs.com/admin",
        oauth_configured=False,
    )
    assert ok is False
    assert detail["reason"] == "calendar_oauth_not_configured"


def test_assess_production_rejects_non_https() -> None:
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="production",
        redirect_uri="http://semantaix.flexsentlabs.com/api/calendar/oauth/callback",
        web_ui_base_url="https://semantaix.flexsentlabs.com/admin",
        oauth_configured=True,
    )
    assert ok is False
    assert detail["reason"] == "redirect_not_https"


def test_assess_production_rejects_path_mismatch() -> None:
    ok, detail = assess_calendar_oauth_production_readiness(
        app_env="production",
        redirect_uri="https://semantaix.flexsentlabs.com/wrong/callback",
        web_ui_base_url="https://semantaix.flexsentlabs.com/admin",
        oauth_configured=True,
    )
    assert ok is False
    assert detail["reason"] == "redirect_path_mismatch"
