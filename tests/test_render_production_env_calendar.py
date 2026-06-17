import os
from pathlib import Path

from scripts.render_production_env import render


def test_render_production_env_sets_calendar_oauth_redirect(tmp_path: Path) -> None:
    base = tmp_path / ".env.production"
    base.write_text(
        "APP_ENV=production\n"
        "WEB_UI_BASE_URL=https://old.example/admin\n"
        "GOOGLE_OAUTH_REDIRECT_URI=https://old.example/callback\n",
        encoding="utf-8",
    )
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "OPENROUTER_API_KEY": "k",
        "TELEGRAM_ALERT_CHAT_ID": "1",
        "TELEGRAM_API_ID": "2",
        "TELEGRAM_API_HASH": "h",
        "INTERNAL_SERVICE_TOKEN": "s",
        "DEPLOY_DOMAIN": "semantaix.flexsentlabs.com",
    }
    old = {key: os.environ.get(key) for key in env}
    try:
        os.environ.update(env)
        rendered = render(base_path=base, domain="semantaix.flexsentlabs.com")
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert (
        "GOOGLE_OAUTH_REDIRECT_URI=https://semantaix.flexsentlabs.com/api/calendar/oauth/callback"
        in rendered
    )
