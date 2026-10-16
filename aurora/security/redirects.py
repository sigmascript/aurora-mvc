"""Opt-in local redirects for destinations derived from user input."""

from urllib.parse import urlsplit

from aurora.core.response import redirect


def redirect_local(path: str, status_code: int = 303):
    """Redirect to a local absolute path, rejecting external/ambiguous URLs.

    Use for a user-supplied ``next`` destination. For trusted application
    destinations, the ordinary ``redirect`` helper remains available.
    """
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("Redirect destination must be a local absolute path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path) or "\\" in path:
        raise ValueError("Redirect destination contains unsafe characters")
    # Encoded separators can be interpreted differently by browsers/proxies.
    lowered = path.lower()
    if "%2f" in lowered or "%5c" in lowered or "%0d" in lowered or "%0a" in lowered:
        raise ValueError("Redirect destination contains encoded separators or controls")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment:
        raise ValueError("Redirect destination must not contain an origin or fragment")
    return redirect(path, status_code=status_code)
