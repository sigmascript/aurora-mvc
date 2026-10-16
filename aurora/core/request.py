import asyncio
import json
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qsl

from aurora.core.http_exception import HTTPException


class ClientDisconnect(Exception):
    """The peer disconnected before its HTTP request body was complete."""


class Request:
    """Expose HTTP request metadata and read the body from ASGI receive.

    Body reads are cached, so body(), text(), json(), and form() can reuse the
    received bytes. Without a receive callable, the body is empty.
    """

    def __init__(self, scope, receive=None, *, max_body_bytes=None):
        if max_body_bytes is not None and (
            isinstance(max_body_bytes, bool) or not isinstance(max_body_bytes, int)
            or max_body_bytes < 0
        ):
            raise ValueError("max_body_bytes must be a non-negative integer or None")
        self.max_body_bytes = max_body_bytes
        self.scope = scope
        self.receive = receive
        self._body = None
        self._body_lock = asyncio.Lock()
        self._session = None
        self.locale = None
        self.preferred_locale = None
        self.routing_path = self.path

    @property
    def session(self):
        """Return the request-local session when session support is enabled.

        Aurora's session manager will attach this before route handling.
        """
        if self._session is None:
            raise RuntimeError("Sessions are not configured for this request")
        return self._session

    @property
    def method(self) -> str:
        """Return the uppercase HTTP method, defaulting to GET."""
        return self.scope.get("method", "GET").upper()

    @property
    def path(self) -> str:
        """Return the request path, defaulting to /."""
        return self.scope.get("path", "/")

    @property
    def query_string(self) -> str:
        """Return the unparsed query string decoded as UTF-8."""
        return self.scope.get("query_string", b"").decode("utf-8")

    @property
    def headers(self) -> dict[str, str]:
        """Return Latin-1 decoded headers with lowercase names.

        If a name occurs more than once, the last value is retained.
        """
        return {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in self.scope.get("headers", [])
        }

    @property
    def cookies(self) -> dict[str, str]:
        """Return parsed cookie values; missing or malformed cookies yield {}.

        Repeated names keep the last value. Values are untrusted client data.
        """
        values = [
            value.decode("latin-1")
            for name, value in self.scope.get("headers", [])
            if name.lower() == b"cookie"
        ]
        cookies = SimpleCookie()

        try:
            cookies.load("; ".join(values))
        except CookieError:
            return {}

        return {name: morsel.value for name, morsel in cookies.items()}

    async def body(self) -> bytes:
        """Read and cache body chunks, enforcing the optional byte limit.

        Only one coroutine may consume the ASGI receive stream at a time.
        Concurrent callers share the completed body rather than racing reads.
        """
        if self._body is not None:
            return self._body

        async with self._body_lock:
            # Another reader may have finished while we waited for the lock.
            if self._body is not None:
                return self._body

            if self.receive is None:
                self._body = b""
                return self._body

            chunks = []
            total_bytes = 0

            while True:
                message = await self.receive()

                if message["type"] == "http.disconnect":
                    raise ClientDisconnect("Client disconnected during request body")
                if message["type"] != "http.request":
                    raise RuntimeError("Unexpected ASGI message while reading request body")

                chunk = message.get("body", b"")
                if not isinstance(chunk, bytes):
                    raise RuntimeError("ASGI request body must be bytes")

                total_bytes += len(chunk)
                if self.max_body_bytes is not None and total_bytes > self.max_body_bytes:
                    raise HTTPException(status_code=413, detail="Request body too large")

                chunks.append(chunk)
                if not message.get("more_body", False):
                    break

            # Do not cache incomplete bodies after disconnects or errors.
            self._body = b"".join(chunks)
            return self._body

    async def text(self) -> str:
        """Decode the body as UTF-8; invalid bytes raise UnicodeDecodeError."""
        body = await self.body()
        return body.decode("utf-8")

    async def json(self) -> Any:
        """Parse the body as JSON.

        Invalid JSON or UTF-8 raises HTTPException with status 400.
        """
        try:
            text = await self.text()
            return json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Invalid JSON")

    async def form(self) -> dict[str, str]:
        """Parse a UTF-8 application/x-www-form-urlencoded body.

        Blank values are retained; repeated names keep the last value.
        Unsupported media types raise HTTPException with status 415.
        Invalid UTF-8 or more than 1000 fields raises status 400.
        Multipart forms and file uploads are not supported.
        """
        content_type = self.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()

        if media_type != "application/x-www-form-urlencoded":
            raise HTTPException(
                status_code=415, detail="Unsupported form content type"
            )

        try:
            text = await self.text()
            fields = parse_qsl(
                text,
                keep_blank_values=True,
                encoding="utf-8",
                errors="strict",
                max_num_fields=1000,
            )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid form data")

        return dict(fields)
