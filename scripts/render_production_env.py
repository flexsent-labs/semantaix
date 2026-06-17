#!/usr/bin/env python3
"""Render production .env from .env.production plus deploy-time secret env vars."""
from __future__ import annotations

import os
import sys
from pathlib import Path

SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OPENROUTER_API_KEY",
    "TELEGRAM_ALERT_CHAT_ID",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "INTERNAL_SERVICE_TOKEN",
    "CALENDAR_TOKEN_ENCRYPTION_KEY",
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
)

REQUIRED_SECRET_KEYS = (
    "TELEGRAM_BOT_TOKEN",
    "OPENROUTER_API_KEY",
    "TELEGRAM_ALERT_CHAT_ID",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "INTERNAL_SERVICE_TOKEN",
)


def _parse_env_lines(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            rows.append(("", line))
            continue
        key, value = line.split("=", 1)
        rows.append((key.strip(), value))
    return rows


def _emit(rows: list[tuple[str, str]]) -> str:
    out: list[str] = []
    for key, value in rows:
        if key:
            out.append(f"{key}={value}")
        else:
            out.append(value)
    return "\n".join(out) + "\n"


def render(*, base_path: Path, domain: str) -> str:
    rows = _parse_env_lines(base_path.read_text(encoding="utf-8"))
    values = {key: value for key, value in rows if key}

    for key in SECRET_KEYS:
        incoming = os.environ.get(key)
        if incoming is not None and incoming != "":
            values[key] = incoming

    missing = [key for key in REQUIRED_SECRET_KEYS if not values.get(key)]
    if missing:
        print(f"ERROR: missing required secret(s): {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(1)

    values["APP_ENV"] = "production"
    values["LOG_FORMAT"] = "json"
    values["WEB_UI_BASE_URL"] = f"https://{domain}/admin"
    values["GOOGLE_OAUTH_REDIRECT_URI"] = f"https://{domain}/api/calendar/oauth/callback"
    values["WEB_UI_ADMIN_COOKIE_SECURE"] = "true"
    values["TELEGRAM_PLATFORM_BOT_USERNAME"] = "@semantaix_bot"
    values.pop("HITL_PRIMARY_OPERATOR_USERNAME", None)
    values.pop("HITL_PRIMARY_OPERATOR_CHAT_ID", None)

    rendered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for key, old_value in rows:
        if not key:
            rendered.append(("", old_value))
            continue
        if key in {"HITL_PRIMARY_OPERATOR_USERNAME", "HITL_PRIMARY_OPERATOR_CHAT_ID"}:
            continue
        rendered.append((key, values.get(key, "")))
        seen.add(key)

    for key, value in values.items():
        if key not in seen:
            rendered.append((key, value))

    return _emit(rendered)


def main() -> None:
    repo_root = Path(os.environ.get("REPO_ROOT", Path(__file__).resolve().parents[1]))
    base_path = Path(os.environ.get("ENV_PRODUCTION_BASE", repo_root / ".env.production"))
    domain = os.environ.get("DEPLOY_DOMAIN", "semantaix.flexsentlabs.com")
    sys.stdout.write(render(base_path=base_path, domain=domain))


if __name__ == "__main__":
    main()
