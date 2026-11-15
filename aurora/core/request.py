import asyncio
import json
import re
from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default as email_policy
from collections.abc import Mapping
from types import MappingProxyType
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qsl

from aurora.core.http_exception import HTTPException


MULTIPART_MAX_BYTES = 8 * 1024 * 1024
MULTIPART_MAX_FILE_BYTES = 4 * 1024 * 1024
MULTIPART_MAX_PARTS = 100


@dataclass(frozen=True)
class UploadedFile:
    """Untrusted in-memory multipart file; application controls any storage."""

    filename: str
    content_type: str
    content: bytes

    @property
    def size(self):
        return len(self.content)


class ClientDisconnect(Exception):
    """The peer disconnected before its HTTP request body was complete."""


class QueryParams(Mapping):
    """Immutable query parameters: last value for lookup, all values via getall."""

    def __init__(self, pairs):
        values = {}
        repeated = {}
        for name, value in pairs:
            values[name] = value
            repeated.setdefault(name, []).append(value)
        self._values = MappingProxyType(values)
        self._repeated = MappingProxyType({key: tuple(items) for key, items in repeated.items()})

    def __getitem__(self, key):
        return self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def getall(self, key):
        """Return all values for a key in wire order, or an empty tuple."""
        return self._repeated.get(key, ())


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
        self._query_params = None
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
    def query_params(self) -> QueryParams:
        """Read-only decoded query fields; duplicates use the last value.

        getall(name) returns every value in arrival order. Invalid UTF-8,
        malformed percent escapes, and more than 1000 fields yield HTTP 400.
        """
        if self._query_params is None:
            try:
                raw = self.scope.get("query_string", b"")
                value = raw.decode("utf-8", errors="strict")
                if re.search(r"%(?![0-9A-Fa-f]{2})", value):
                    raise ValueError("Invalid percent encoding")
                pairs = parse_qsl(value, keep_blank_values=True, encoding="utf-8",
                                  errors="strict", max_num_fields=1000)
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(400, "Invalid query string") from None
            self._query_params = QueryParams(pairs)
        return self._query_params

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

    async def body(self, *, max_bytes=None) -> bytes:
        """Read and cache body chunks, enforcing the optional byte limit.

        Only one coroutine may consume the ASGI receive stream at a time.
        Concurrent callers share the completed body rather than racing reads.
        """

        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
            raise ValueError("max_bytes must be a non-negative integer")
        effective_limit = self.max_body_bytes
        if max_bytes is not None:
            effective_limit = min(max_bytes, effective_limit) if effective_limit is not None else max_bytes

        if self._body is not None:
            if effective_limit is not None and len(self._body) > effective_limit:
                raise HTTPException(413, "Request body too large")
            return self._body

        async with self._body_lock:
            # Another reader may have finished while we waited for the lock.
            if self._body is not None:
                if effective_limit is not None and len(self._body) > effective_limit:
                    raise HTTPException(413, "Request body too large")
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

                if effective_limit is not None and total_bytes > effective_limit:
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

    async def form(self) -> dict[str, str | UploadedFile]:
        """Parse bounded URL-encoded or multipart form data.

        Multipart uploads are in-memory and never stored automatically. A
        multipart request is limited to 8 MiB total, 4 MiB per file, and
        100 fields/files. Duplicate names retain the last value. Filenames
        and media types are untrusted and must be checked by applications.
        """
        content_type = self.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()

        if media_type == "application/x-www-form-urlencoded":
            try:
                text = await self.text()
                fields = parse_qsl(
                    text, keep_blank_values=True, encoding="utf-8",
                    errors="strict", max_num_fields=1000,
                )
            except (ValueError, UnicodeDecodeError):
                raise HTTPException(400, "Invalid form data") from None
            return dict(fields)

        if media_type != "multipart/form-data":
            raise HTTPException(415, "Unsupported form content type")

        # The bounded body read also checks cached data that another API read
        # first, so calling body() before form() cannot bypass upload limits.
        body = await self.body(max_bytes=MULTIPART_MAX_BYTES)
        try:
            header = content_type.encode("ascii", "strict")
            if b"\r" in header or b"\n" in header:
                raise ValueError("Header injection")
            message = BytesParser(policy=email_policy).parsebytes(
                b"MIME-Version: 1.0\r\nContent-Type: " + header + b"\r\n\r\n" + body
            )
            boundary = message.get_boundary()
            if not boundary or len(boundary) > 70 or not boundary.isascii():
                raise ValueError("Invalid boundary")
            boundary_bytes = boundary.encode("ascii")
            if not body.startswith(b"--" + boundary_bytes + b"\r\n"):
                raise ValueError("Missing opening boundary")
            if not body.rstrip(b"\r\n").endswith(b"--" + boundary_bytes + b"--"):
                raise ValueError("Missing closing boundary")
            if not message.is_multipart() or message.defects:
                raise ValueError("Invalid multipart")

            parts = message.get_payload()
            if len(parts) > MULTIPART_MAX_PARTS:
                raise HTTPException(413, "Too many form fields")
            result = {}
            for part in parts:
                if part.defects or part.is_multipart() or part.get_content_disposition() != "form-data":
                    raise ValueError("Invalid multipart part")
                name = part.get_param("name", header="content-disposition")
                if not isinstance(name, str) or not name or "\x00" in name:
                    raise ValueError("Invalid form field name")
                if part.get("Content-Transfer-Encoding") not in (None, "binary", "8bit", "7bit"):
                    raise ValueError("Unsupported transfer encoding")
                data = part.get_payload(decode=True)
                if not isinstance(data, bytes):
                    raise ValueError("Invalid part payload")
                filename = part.get_param("filename", header="content-disposition")
                if filename is not None:
                    if not isinstance(filename, str) or "\x00" in filename:
                        raise ValueError("Invalid filename")
                    if len(data) > MULTIPART_MAX_FILE_BYTES:
                        raise HTTPException(413, "Uploaded file too large")
                    result[name] = UploadedFile(filename, part.get_content_type(), data)
                else:
                    result[name] = data.decode("utf-8", "strict")
            return result
        except (ValueError, UnicodeDecodeError, TypeError, LookupError):
            raise HTTPException(400, "Invalid multipart form data") from None
