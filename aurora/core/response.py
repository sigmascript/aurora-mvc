import json
import re
from http.cookies import SimpleCookie


_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def _header_name(value: str) -> bytes:
    """Encode an HTTP field name, rejecting invalid token characters."""
    if not isinstance(value, str):
        raise ValueError("HTTP header names must be strings")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid HTTP header name") from exc
    if _HEADER_NAME.fullmatch(encoded) is None:
        raise ValueError("Invalid HTTP header name")
    return encoded.lower()


def _header_value(value: str) -> bytes:
    """Encode a field value without allowing control-character injection."""
    try:
        encoded = value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError("Invalid HTTP header value") from exc
    if any(byte < 32 and byte != 9 or byte == 127 for byte in encoded):
        raise ValueError("Invalid HTTP header value")
    return encoded


class Response:
    """Send string content as a UTF-8 encoded HTTP response.

    Set content_type to control the media type. Custom headers cannot
    override the generated Content-Type or Content-Length headers.
    """

    def __init__(
        self,
        content: str = "",
        status_code: int = 200,
        content_type: str = "text/plain; charset=utf-8",
        headers=None,
    ):
        self.content = content
        self.status_code = status_code
        self.content_type = content_type
        self.headers = headers or {}

        # Preserve individual Set-Cookie fields and replace by name/path.
        self._cookie_headers = {}

    def set_cookie(
        self,
        name: str,
        value: str,
        *,
        path: str = "/",
        max_age: int | None = None,
        secure: bool = False,
        httponly: bool = True,
        samesite: str = "Lax",
    ) -> None:
        """Set an ASCII cookie with HttpOnly and SameSite=Lax by default.

        Secure defaults to False for local HTTP; enable it with HTTPS.
        SameSite=None requires Secure. Reusing a name/path replaces its
        pending header. Omit max_age for a browser-session cookie.
        """
        if not isinstance(value, str) or not value.isascii():
            raise ValueError("Cookie values must be ASCII strings")

        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Cookie values cannot contain control characters")
        if not path or not path.isascii() or any(
            ord(char) < 32 or ord(char) == 127 or char == ";" for char in path
        ):
            raise ValueError("Invalid cookie path")

        if max_age is not None and (
            not isinstance(max_age, int) or isinstance(max_age, bool)
        ):
            raise ValueError("Cookie max_age must be an integer")

        if not isinstance(samesite, str) or samesite.lower() not in {
            "lax", "strict", "none"
        }:
            raise ValueError("Invalid cookie SameSite value")
        if samesite.lower() == "none" and not secure:
            raise ValueError("SameSite=None requires Secure")

        cookies = SimpleCookie()
        cookies[name] = value
        cookie = cookies[name]
        cookie["path"] = path
        cookie["secure"] = secure
        cookie["httponly"] = httponly
        cookie["samesite"] = samesite.capitalize()
        if max_age is not None:
            cookie["max-age"] = max_age

        self._cookie_headers[(name, path)] = cookie.OutputString().encode("ascii")

    def delete_cookie(
        self,
        name: str,
        *,
        path: str = "/",
        secure: bool = False,
        httponly: bool = True,
        samesite: str = "Lax",
    ) -> None:
        """Expire a cookie; path must match the path used when setting it."""
        self.set_cookie(
            name, "", path=path, max_age=0, secure=secure,
            httponly=httponly, samesite=samesite,
        )
        self._cookie_headers[(name, path)] += (
            b"; expires=Thu, 01 Jan 1970 00:00:00 GMT"
        )

    def prepare(self):
        """Validate and serialize both ASGI messages before transmission."""
        body = self.content.encode("utf-8")
        body_forbidden = 100 <= self.status_code < 200 or self.status_code in {204, 304}

        # Bodyless status codes must not emit a payload or misleading entity
        # headers. HEAD is handled by Application at the ASGI boundary.
        headers = []
        if not body_forbidden:
            headers.extend([
                [b"content-type", _header_value(self.content_type)],
                [b"content-length", str(len(body)).encode("ascii")],
            ])

        if body_forbidden:
            body = b""

        # Preserve caller-provided headers except generated content headers.
        for key, value in self.headers.items():
            normalized_key = _header_name(key)
            normalized_value = _header_value(str(value))

            # Content headers must agree with the body and configured media type.
            if normalized_key in {b"content-type", b"content-length"}:
                continue

            headers.append([normalized_key, normalized_value])

        # Set-Cookie fields must remain separate rather than comma-joined.
        headers.extend(
            [b"set-cookie", value] for value in self._cookie_headers.values()
        )

        return (
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": headers,
            },
            {
                "type": "http.response.body",
                "body": body,
            },
        )

    async def __call__(self, send) -> None:
        """Send a fully prepared response without retrying transport failures."""
        start, body = self.prepare()
        await send(start)
        await send(body)


class JSONResponse(Response):
    """Serialize JSON-compatible content, preserving Unicode characters."""

    def __init__(self, content, status_code: int = 200, headers=None):
        serialized_content = json.dumps(content, ensure_ascii=False)

        super().__init__(
            content=serialized_content,
            status_code=status_code,
            content_type="application/json; charset=utf-8",
            headers=headers,
        )


class RedirectResponse(Response):
    """Send an empty body and a Location header, defaulting to status 302.

    Supplied headers are copied before the Location entry is set.
    """

    def __init__(self, location: str, status_code: int = 302, headers=None):
        response_headers = dict(headers or {})
        response_headers["Location"] = location

        super().__init__(
            content="",
            status_code=status_code,
            headers=response_headers,
        )


def redirect(path: str, status_code: int = 303) -> RedirectResponse:
    """Redirect to a URL; default 303 is suitable after HTML form POSTs."""
    return RedirectResponse(path, status_code=status_code)
