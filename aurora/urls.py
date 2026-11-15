"""Named-route URL generation for Aurora applications."""

from contextvars import ContextVar
import math
from collections.abc import Mapping
from urllib.parse import urlencode

_current_application = ContextVar("aurora_current_application", default=None)


def url_for(name, **params):
    """Generate a URL inside a request handler; outside use app.url_for()."""
    app = _current_application.get()

    if app is None:
        raise RuntimeError(
            "url_for() requires an active request; use app.url_for() outside handlers"
        )

    return app.url_for(name, **params)


def with_query(url, fields):
    """Append encoded query pairs without changing a route path or fragment.

    ``fields`` is a mapping (with optional list/tuple values) or a sequence
    of ``(key, value)`` pairs for explicit ordering and repeated keys.
    Empty strings are retained; None and non-scalar values are rejected.
    """
    if not isinstance(url, str):
        raise TypeError("URL must be a string")

    pairs = fields.items() if isinstance(fields, Mapping) else fields
    if isinstance(pairs, (str, bytes)):
        raise TypeError("Query fields must be a mapping or a sequence of pairs")

    try:
        iterator = iter(pairs)
    except TypeError:
        raise TypeError("Query fields must be a mapping or a sequence of pairs") from None

    normalized = []
    for pair in iterator:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise TypeError("Query fields must contain key-value pairs")
        key, value = pair
        values = value if isinstance(value, (list, tuple)) else (value,)
        for item in values:
            if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
                raise TypeError("Query keys must be strings and values must be scalars")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Query values must be finite")
            normalized.append((key, str(item).lower() if isinstance(item, bool) else str(item)))

    encoded = urlencode(normalized)
    if not encoded:
        return url
    before_fragment, separator, fragment = url.partition("#")
    joiner = "&" if "?" in before_fragment and not before_fragment.endswith("?") else "" if before_fragment.endswith("?") else "?"
    return before_fragment + joiner + encoded + (separator + fragment if separator else "")
