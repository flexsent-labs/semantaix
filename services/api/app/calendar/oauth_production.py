"""Production readiness checks for Google Calendar OAuth redirect configuration."""

from __future__ import annotations

from urllib.parse import urlparse

CALENDAR_OAUTH_CALLBACK_PATH = "/api/calendar/oauth/callback"

# Substrings that indicate a dev/tunnel redirect unsuitable for production deploy.
_DEV_REDIRECT_MARKERS = (
    "ngrok",
    "localhost",
    "127.0.0.1",
    ".local",
    "trycloudflare.com",
)


def expected_calendar_redirect_uri(*, host: str) -> str:
    host = host.strip().lower().removeprefix("https://").removeprefix("http://")
    host = host.split("/", 1)[0]
    return f"https://{host}{CALENDAR_OAUTH_CALLBACK_PATH}"


def host_from_web_ui_base_url(web_ui_base_url: str | None) -> str | None:
    if not web_ui_base_url:
        return None
    parsed = urlparse(web_ui_base_url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return parsed.netloc


def is_dev_redirect_uri(redirect_uri: str | None) -> bool:
    if not redirect_uri:
        return True
    lower = redirect_uri.strip().lower()
    return any(marker in lower for marker in _DEV_REDIRECT_MARKERS)


def assess_calendar_oauth_production_readiness(
    *,
    app_env: str,
    redirect_uri: str | None,
    web_ui_base_url: str | None,
    oauth_configured: bool,
) -> tuple[bool, dict[str, object]]:
    """Return (ok, detail) for monitoring and deploy probes."""
    if app_env != "production":
        return True, {"prod_ready": False, "reason": "non_production", "app_env": app_env}

    if not oauth_configured:
        return False, {
            "prod_ready": False,
            "reason": "calendar_oauth_not_configured",
        }

    redirect = (redirect_uri or "").strip()
    host = host_from_web_ui_base_url(web_ui_base_url)
    expected = expected_calendar_redirect_uri(host=host) if host else None

    if not redirect.startswith("https://"):
        return False, {
            "prod_ready": False,
            "reason": "redirect_not_https",
            "redirect_uri": redirect,
        }

    if is_dev_redirect_uri(redirect):
        return False, {
            "prod_ready": False,
            "reason": "dev_redirect_in_production",
            "redirect_uri": redirect,
        }

    if not redirect.endswith(CALENDAR_OAUTH_CALLBACK_PATH):
        return False, {
            "prod_ready": False,
            "reason": "redirect_path_mismatch",
            "redirect_uri": redirect,
            "expected_path": CALENDAR_OAUTH_CALLBACK_PATH,
        }

    if expected is not None and redirect != expected:
        return False, {
            "prod_ready": False,
            "reason": "redirect_host_mismatch",
            "redirect_uri": redirect,
            "expected_redirect_uri": expected,
        }

    return True, {
        "prod_ready": True,
        "redirect_uri": redirect,
        "expected_redirect_uri": expected,
    }
