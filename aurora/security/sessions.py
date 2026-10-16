"""SQLite-backed, opaque-token server-side HTTP sessions.

SQLite transactions serialize competing writers. A version check prevents stale
requests from restoring data after logout or overwriting another request.
"""

import asyncio
import hashlib
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path

from aurora.core.session import Session


_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class SessionStorageError(RuntimeError):
    """Stored session data is invalid or cannot be safely used."""


class SessionConflict(RuntimeError):
    """The session was changed or revoked by another request."""


@dataclass(frozen=True)
class SessionSettings:
    """Configuration for an opt-in SQLite session store.

    Use secure=True behind HTTPS in production. The path must be writable.
    """

    database_path: str
    cookie_name: str = "aurora_session"
    lifetime: int = 3600
    secure: bool = True
    samesite: str = "Lax"
    max_data_bytes: int = 16384

    def __post_init__(self):
        if not self.database_path or self.database_path == ":memory:":
            raise ValueError("Sessions require a persistent SQLite database path")
        if not isinstance(self.lifetime, int) or isinstance(self.lifetime, bool) or self.lifetime <= 0:
            raise ValueError("Session lifetime must be a positive integer")
        if not isinstance(self.cookie_name, str) or not _COOKIE_NAME.fullmatch(self.cookie_name):
            raise ValueError("Invalid session cookie name")
        if self.samesite not in ("Lax", "Strict", "None"):
            raise ValueError("Invalid SameSite policy")
        if self.samesite == "None" and not self.secure:
            raise ValueError("SameSite=None requires secure cookies")
        if not isinstance(self.secure, bool):
            raise ValueError("secure must be a boolean")
        if not isinstance(self.max_data_bytes, int) or isinstance(self.max_data_bytes, bool) or self.max_data_bytes <= 0:
            raise ValueError("max_data_bytes must be a positive integer")
        if self.cookie_name.startswith(("__Host-", "__Secure-")) and not self.secure:
            raise ValueError("__Host- and __Secure- cookies require secure=True")


class SQLiteSessionStore:
    def __init__(self, path):
        self.path = str(Path(path))
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self):
        with closing(self._connect()) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS aurora_sessions (
                token_hash TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                expires_at REAL NOT NULL,
                version INTEGER NOT NULL
            )""")

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def load(self, token):
        if not token or len(token) > 256 or not token.isascii():
            return None
        with closing(self._connect()) as db, db:
            row = db.execute("SELECT data, expires_at, version FROM aurora_sessions WHERE token_hash=?", (self._hash(token),)).fetchone()
        if row is None or row[1] <= time.time():
            return None
        try:
            data = json.loads(row[0])
            if not isinstance(data, dict):
                raise ValueError("Session record is not an object")
        except (ValueError, TypeError) as exc:
            raise SessionStorageError("Invalid stored session data") from exc
        return data, row[2]

    def cleanup_expired(self, *, limit=1000, now=None):
        """Delete at most ``limit`` expired sessions; never touch active sessions.

        This is explicit maintenance, not a request-time operation. Call it
        periodically from an operator-owned maintenance job when needed.
        """
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if now is None:
            now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            result = db.execute(
                "DELETE FROM aurora_sessions WHERE token_hash IN ("
                "SELECT token_hash FROM aurora_sessions WHERE expires_at <= ? "
                "ORDER BY expires_at, token_hash LIMIT ?)",
                (now, limit),
            )
            return result.rowcount

    def commit(self, old_token, version, data, *, rotate=False, invalidate=False, lifetime=3600):
        """Atomically revoke/update/rotate a token with optimistic locking."""
        new_token = None
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if old_token is not None:
                row = db.execute("SELECT version, expires_at FROM aurora_sessions WHERE token_hash=?", (self._hash(old_token),)).fetchone()
                if row is None or row[0] != version or row[1] <= time.time():
                    raise SessionConflict("Session has expired or changed")
            if invalidate:
                if old_token is not None:
                    db.execute("DELETE FROM aurora_sessions WHERE token_hash=?", (self._hash(old_token),))
                return None
            if rotate or old_token is None:
                if old_token is not None:
                    db.execute("DELETE FROM aurora_sessions WHERE token_hash=?", (self._hash(old_token),))
                new_token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO aurora_sessions VALUES (?, ?, ?, ?)", (
                    self._hash(new_token), json.dumps(data, allow_nan=False), time.time() + lifetime, 1))
            else:
                result = db.execute("UPDATE aurora_sessions SET data=?, version=version+1 WHERE token_hash=? AND version=?", (
                    json.dumps(data, allow_nan=False), self._hash(old_token), version))
                if result.rowcount != 1:
                    raise SessionConflict("Session was changed")
        return new_token


class SessionManager:
    def __init__(self, settings):
        self.settings = settings
        self.store = SQLiteSessionStore(settings.database_path)

    async def load(self, request):
        token = request.cookies.get(self.settings.cookie_name)
        record = await asyncio.to_thread(self.store.load, token) if token else None
        session = Session(record[0] if record else None)
        session._token = token if record else None
        session._version = record[1] if record else None
        # Unknown/expired cookies should be cleared even on read-only requests.
        session._stale_cookie = bool(token and not record)
        request._session = session

    async def save(self, request, response):
        # The session manager exclusively owns its cookie. A handler must not
        # forge or overwrite it through response headers or cookie helpers.
        name = self.settings.cookie_name
        for key in response.headers:
            if key.lower() == "set-cookie":
                raise SessionStorageError("Use response.set_cookie() for application cookies")
        if any(key[0] == name for key in response._cookie_headers):
            raise SessionStorageError("Session cookie is reserved for Aurora")
        session = request.session
        token = session._token
        if session.invalidated:
            await asyncio.to_thread(self.store.commit, token, session._version, {}, invalidate=True)
            response.delete_cookie(self.settings.cookie_name, secure=self.settings.secure, samesite=self.settings.samesite)
            return
        if not session.changed and not session.rotation_requested:
            if session._stale_cookie:
                response.delete_cookie(self.settings.cookie_name, secure=self.settings.secure, samesite=self.settings.samesite)
            return
        # A new empty session is not persisted unless rotation was requested.
        if token is None and not session.rotation_requested and not dict(session):
            if session._stale_cookie:
                response.delete_cookie(self.settings.cookie_name, secure=self.settings.secure, samesite=self.settings.samesite)
            return
        data = dict(session)
        if len(json.dumps(data, allow_nan=False).encode("utf-8")) > self.settings.max_data_bytes:
            raise SessionStorageError("Session data exceeds configured size limit")
        new_token = await asyncio.to_thread(
            self.store.commit, token, session._version, data,
            rotate=session.rotation_requested, lifetime=self.settings.lifetime,
        )
        if new_token is not None:
            response.set_cookie(self.settings.cookie_name, new_token, secure=self.settings.secure,
                                httponly=True, samesite=self.settings.samesite, max_age=self.settings.lifetime)
