"""Validate local post-authentication return destinations."""

from urllib.parse import parse_qsl, quote, unquote, urlsplit

from aurora.security.redirects import redirect_local


def safe_next(value, fallback="/"):
    """Return a validated local path, or a trusted local fallback.

    Treat ``value`` as untrusted user input. The fallback must itself be safe;
    an invalid fallback is a programmer error rather than an open redirect.
    """
    _check_local(fallback)
    try:
        _check_local(value)
    except (TypeError, ValueError):
        return fallback
    return value


def _check_local(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Return URL must be a local path of at most 2048 characters")
    # Reject Unicode/percent-encoded controls and ambiguous separators, even
    # when a proxy decodes more than once. Limit depth to avoid excess work.
    decoded = value
    for _ in range(4):
        if any(ord(c) < 32 or ord(c) == 127 for c in decoded):
            raise ValueError("Unsafe return URL control character")
        if "\\" in decoded or decoded.startswith("//"):
            raise ValueError("Unsafe return URL separator")
        parts = urlsplit(decoded)
        if parts.scheme or parts.netloc or parts.fragment or not parts.path.startswith("/"):
            raise ValueError("Return URL must be an absolute local path")
        next_decoded = unquote(decoded, errors="strict")
        if next_decoded == decoded:
            break
        decoded = next_decoded
    else:
        raise ValueError("Return URL has excessive encoding")
    redirect_local(value)  # Existing Aurora redirect policy remains authoritative.


def request_next(request, fallback="/"):
    """Read and validate one `next` query parameter from an incoming request."""
    try:
        pairs = parse_qsl(
            request.query_string,
            keep_blank_values=True,
            errors="strict",
            max_num_fields=100,
        )
    except (UnicodeError, ValueError):
        return safe_next(None, fallback)
    candidates = [value for key, value in pairs if key == "next"]
    if len(candidates) != 1:
        return safe_next(None, fallback)
    return safe_next(candidates[0], fallback)


def append_next(destination, requested_path):
    """Add a validated return URL to a trusted sign-in route URL."""
    _check_local(destination)
    _check_local(requested_path)
    separator = "&" if "?" in destination else "?"
    return destination + separator + "next=" + quote(requested_path, safe="")
