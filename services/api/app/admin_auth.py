"""Admin/operator auth — schema + session-cookie routes + Epic 10 login flow.

Three complementary pieces live here:

- ``AdminAuthRepository`` owns the schema PLUS the full Epic 10 story
  10.02 login-code lifecycle: request_code / consume_code /
  validate_session / revoke_session / purge_expired. Codes are 6-digit
  numeric, session tokens are ``secrets.token_urlsafe(32)`` — both
  sha256-hashed before storage; plaintext returned exactly once.
- ``AdminAuthService`` + ``wire_admin_auth_routes`` implement the
  inspect-extracted-text feature's auth surface: four endpoints
  (request_code, verify, me, logout) plus a ``require_session`` FastAPI
  dependency that returns a ``SessionPrincipal``. A second dependency
  ``require_session_or_internal`` lets internal services (currently the
  bot_gateway) bypass the cookie by passing
  ``Authorization: Bearer <internal_service_token>`` with an ``as_user``
  query parameter, so bot commands can scope ``/admin/files`` to the
  requesting user.
- Epic 10 endpoints in ``services/api/app/main.py`` use
  ``AdminAuthRepository`` directly via ``require_admin_session``; the
  ``AdminAuthService`` path uses ``WebAuthRepository`` for code/session
  state and is owned by the inspect-files feature.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from services.api.app.operator_chat_lookup import resolve_chat_id_for_username
from services.api.app.web_auth import WebAuthRepository

_CODE_DIGITS = "0123456789"
_CODE_LENGTH = 6
_TOKEN_NBYTES = 32


class InvalidLoginCode(Exception):
    """Raised when an admin login code cannot be consumed.

    Includes: unknown admin, wrong code, expired code, already-consumed
    code (replay). The handler maps this to an HTTP 401 without revealing
    which branch failed.
    """


@dataclass(frozen=True)
class AdminSession:
    token: str
    admin_username: str
    expires_at: str


def _connect(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    return connection


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _generate_code() -> str:
    return "".join(secrets.choice(_CODE_DIGITS) for _ in range(_CODE_LENGTH))


def _generate_token() -> str:
    return secrets.token_urlsafe(_TOKEN_NBYTES)


class AdminAuthRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self.init_schema()

    def init_schema(self) -> None:
        with _connect(self.db_path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_login_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_username TEXT NOT NULL,
                    code_sha256 TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_admin_codes_username "
                "ON admin_login_codes(admin_username)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_sessions (
                    token_sha256 TEXT PRIMARY KEY,
                    admin_username TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def request_code(self, *, admin_username: str, ttl_seconds: int) -> str:
        now = _now()
        expires_at = _iso(now + timedelta(seconds=ttl_seconds))
        code = _generate_code()
        code_hash = _sha256(code)
        with _connect(self.db_path) as connection:
            # Invalidate prior unconsumed codes for the same admin.
            connection.execute(
                """
                UPDATE admin_login_codes
                SET consumed_at = ?
                WHERE admin_username = ? AND consumed_at IS NULL
                """,
                (_iso(now), admin_username),
            )
            connection.execute(
                """
                INSERT INTO admin_login_codes
                    (admin_username, code_sha256, expires_at, consumed_at, created_at)
                VALUES (?, ?, ?, NULL, ?)
                """,
                (admin_username, code_hash, expires_at, _iso(now)),
            )
        return code

    def consume_code(
        self, *, admin_username: str, code: str, ttl_seconds: int
    ) -> AdminSession:
        now = _now()
        provided_hash = _sha256(code)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT id, code_sha256, expires_at
                FROM admin_login_codes
                WHERE admin_username = ? AND consumed_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (admin_username,),
            ).fetchone()
            if row is None:
                raise InvalidLoginCode("no_pending_code")
            stored_hash = str(row["code_sha256"])
            if not hmac.compare_digest(stored_hash, provided_hash):
                raise InvalidLoginCode("code_mismatch")
            expires_at = datetime.fromisoformat(str(row["expires_at"]))
            if expires_at <= now:
                raise InvalidLoginCode("code_expired")
            connection.execute(
                "UPDATE admin_login_codes SET consumed_at = ? WHERE id = ?",
                (_iso(now), int(row["id"])),
            )
            token = _generate_token()
            session_expiry = _iso(now + timedelta(seconds=ttl_seconds))
            connection.execute(
                """
                INSERT INTO admin_sessions
                    (token_sha256, admin_username, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (_sha256(token), admin_username, session_expiry, _iso(now)),
            )
        return AdminSession(
            token=token, admin_username=admin_username, expires_at=session_expiry
        )

    def validate_session(self, token: str) -> AdminSession | None:
        token_hash = _sha256(token)
        with _connect(self.db_path) as connection:
            row = connection.execute(
                """
                SELECT admin_username, expires_at
                FROM admin_sessions
                WHERE token_sha256 = ?
                """,
                (token_hash,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at <= _now():
            return None
        return AdminSession(
            token=token,
            admin_username=str(row["admin_username"]),
            expires_at=str(row["expires_at"]),
        )

    def revoke_session(self, token: str) -> None:
        token_hash = _sha256(token)
        with _connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE token_sha256 = ?",
                (token_hash,),
            )

    def purge_expired(self) -> int:
        now_iso = _iso(_now())
        with _connect(self.db_path) as connection:
            codes_cursor = connection.execute(
                "DELETE FROM admin_login_codes WHERE expires_at <= ?",
                (now_iso,),
            )
            sessions_cursor = connection.execute(
                "DELETE FROM admin_sessions WHERE expires_at <= ?",
                (now_iso,),
            )
            removed = (codes_cursor.rowcount or 0) + (sessions_cursor.rowcount or 0)
        return removed


class RequestCodeBody(BaseModel):
    username: str


class VerifyBody(BaseModel):
    username: str
    code: str


@dataclass(frozen=True)
class SessionPrincipal:
    username: str
    role: str


def _normalize_username(name: str) -> str:
    candidate = (name or "").strip()
    if not candidate:
        return ""
    if not candidate.startswith("@"):
        candidate = "@" + candidate
    return candidate


class AdminAuthService:
    def __init__(
        self,
        *,
        web_auth_repository: WebAuthRepository,
        telegram_bot_sender,
        settings,
    ) -> None:
        self.web_auth_repository = web_auth_repository
        self.telegram_bot_sender = telegram_bot_sender
        self.settings = settings

    def _resolve_chat_id(self, username: str) -> int | None:
        admin_usernames = {
            self.settings.admin_telegram_username,
            self.settings.hitl_config_admin_username,
        }
        if username in admin_usernames:
            raw = self.settings.telegram_alert_chat_id
            if raw is not None:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    pass
        return resolve_chat_id_for_username(
            username=username,
            operator_files_db_path=self.settings.operator_files_db_path,
            operators_db_path=self.settings.operators_db_path,
        )

    def resolve_role(self, username: str) -> str | None:
        admin_usernames = {
            self.settings.hitl_config_admin_username,
            self.settings.admin_telegram_username,
        }
        if username in admin_usernames:
            return "admin"
        # Anyone with a known chat_id (operator_files or operators table) is an operator.
        if self._resolve_chat_id(username) is not None:
            return "operator"
        return None

    async def request_code(self, raw_username: str) -> dict:
        username = _normalize_username(raw_username)
        if not username:
            raise HTTPException(status_code=404, detail="username_unknown_or_chat_id_missing")
        chat_id = self._resolve_chat_id(username)
        if chat_id is None:
            raise HTTPException(
                status_code=404, detail="username_unknown_or_chat_id_missing"
            )
        code = self.web_auth_repository.create_code(
            username=username, chat_id=chat_id
        )
        await self.telegram_bot_sender.send_message(
            chat_id=chat_id,
            text=f"Код входа в админку: {code} (действует 5 минут).",
        )
        return {"sent": True}

    def verify(
        self, raw_username: str, code: str, response: Response
    ) -> dict:
        username = _normalize_username(raw_username)
        outcome = self.web_auth_repository.consume_code(
            username=username, code=code
        )
        if not outcome.ok:
            if outcome.reason == "expired":
                raise HTTPException(status_code=410, detail="expired")
            if outcome.reason == "too_many_attempts":
                raise HTTPException(status_code=429, detail="too_many_attempts")
            raise HTTPException(status_code=401, detail="invalid")
        role = self.resolve_role(username)
        if role is None:
            raise HTTPException(status_code=403, detail="not_allowed")
        # Rotate sessions for this user — old cookies become invalid.
        self.web_auth_repository.revoke_all_for_username(username=username)
        session_id = self.web_auth_repository.create_session(
            username=username, role=role
        )
        response.set_cookie(
            key=self.settings.web_session_cookie_name,
            value=session_id,
            httponly=True,
            samesite="lax",
            secure=self.settings.web_session_cookie_secure,
            path="/",
        )
        return {"username": username, "role": role}

    def require_session(self, request: Request) -> SessionPrincipal:
        cookie = request.cookies.get(self.settings.web_session_cookie_name)
        if not cookie:
            raise HTTPException(status_code=401, detail="no_session")
        session = self.web_auth_repository.get_session(session_id=cookie)
        if session is None:
            raise HTTPException(status_code=401, detail="invalid_session")
        self.web_auth_repository.touch_session(session_id=cookie)
        return SessionPrincipal(username=session.username, role=session.role)

    def require_session_or_internal(
        self, request: Request, as_user: str | None
    ) -> SessionPrincipal:
        token = self.settings.internal_service_token
        header = request.headers.get("authorization", "")
        if token and header.startswith("Bearer ") and header.removeprefix("Bearer ") == token:
            if not as_user:
                raise HTTPException(
                    status_code=400, detail="missing_as_user"
                )
            username = _normalize_username(as_user)
            role = self.resolve_role(username)
            if role is None:
                raise HTTPException(status_code=403, detail="not_allowed")
            return SessionPrincipal(username=username, role=role)
        return self.require_session(request)


def wire_admin_auth_routes(app: FastAPI, *, service: AdminAuthService) -> None:
    @app.post("/admin/auth/request_code")
    async def _request_code(payload: RequestCodeBody) -> dict:
        return await service.request_code(payload.username)

    @app.post("/admin/auth/verify")
    def _verify(payload: VerifyBody, response: Response) -> dict:
        return service.verify(payload.username, payload.code, response)

    @app.get("/admin/auth/me")
    def _me(request: Request) -> dict:
        principal = service.require_session(request)
        return {"username": principal.username, "role": principal.role}

    @app.post("/admin/auth/logout")
    def _logout(request: Request, response: Response) -> dict:
        cookie = request.cookies.get(service.settings.web_session_cookie_name)
        if cookie:
            service.web_auth_repository.revoke_session(session_id=cookie)
        response.delete_cookie(
            key=service.settings.web_session_cookie_name, path="/"
        )
        return {"ok": True}
