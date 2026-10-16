import re
from urllib.parse import quote

from aurora.core.request import Request


# HTTP method names use the RFC token character set (ASCII only).
_METHOD_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_ROUTE_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class Route:
    """Associate a path and HTTP methods with a handler.

    Use {name} in the path to capture a non-empty segment as a string.
    Methods are normalized to uppercase and default to GET.
    """

    def __init__(self, path, handler, methods=None, name=None):
        if not isinstance(path, str):
            raise TypeError("Route path must be a string")

        # Reject malformed placeholders before compiling a route pattern.
        literal_parts = _ROUTE_PLACEHOLDER.split(path)
        if any("{" in part or "}" in part for part in literal_parts[::2]):
            raise ValueError("Invalid route parameter placeholder")

        parameter_names = literal_parts[1::2]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("Duplicate route parameter name")

        self.path = path
        self.name = name
        self.parameters = parameter_names
        self.handler = handler
        self.methods = self._normalize_methods(methods)
        self.pattern = self._compile_path(path)

    @staticmethod
    def _normalize_methods(methods) -> set[str]:
        """Validate explicit HTTP methods; None alone selects GET."""
        if methods is None:
            return {"GET"}
        if isinstance(methods, (str, bytes)):
            raise TypeError("Route methods must be an iterable of method strings")

        try:
            iterator = iter(methods)
        except TypeError:
            raise TypeError("Route methods must be an iterable of method strings") from None

        normalized = set()
        for method in iterator:
            if not isinstance(method, str) or _METHOD_TOKEN.fullmatch(method) is None:
                raise ValueError("Route methods must contain valid HTTP method strings")
            normalized.add(method.upper())

        if not normalized:
            raise ValueError("Route methods cannot be empty")
        return normalized

    def _compile_path(self, path: str) -> re.Pattern[str]:
        # Escape literal route text so regex metacharacters match themselves.
        parts = []
        position = 0

        for match in _ROUTE_PLACEHOLDER.finditer(path):
            parts.append(re.escape(path[position:match.start()]))
            parts.append(f"(?P<{match.group(1)}>[^/]+)")
            position = match.end()

        parts.append(re.escape(path[position:]))
        return re.compile("^" + "".join(parts) + "$")

    def matches(self, request: Request) -> dict[str, str] | None:
        """Return captured parameters, or None when method or path differs.

        A matching path without parameters returns an empty dictionary.
        """
        if request.method not in self.methods:
            return None

        routing_path = request.routing_path
        if routing_path is None:
            return None
        match = self.pattern.match(routing_path)

        if match is None:
            return None

        return match.groupdict()

    def build_url(self, **params):
        """Build a relative URL, escaping values as individual path segments."""
        required = set(self.parameters)
        provided = set(params)
        if required != provided:
            missing = sorted(required - provided)
            extra = sorted(provided - required)
            raise ValueError(f"Route parameters mismatch: missing={missing}, unexpected={extra}")
        result = self.path

        # Encode each value as one segment; never interpret its URL syntax.
        for key in self.parameters:
            value = str(params[key])
            if not value or value in {".", ".."} or "/" in value or "\\" in value:
                raise ValueError(f"Invalid path segment for {key}")
            result = result.replace("{" + key + "}", quote(value, safe=""))
        return result
