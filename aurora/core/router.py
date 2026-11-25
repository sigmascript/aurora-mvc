import warnings

from aurora.core.request import Request
from aurora.core.route import Route


class Router:
    """Register routes and resolve requests in registration order.

    Named routes support URL generation without hardcoded application paths.
    """

    def __init__(self):
        self.routes = []
        self.named_routes = {}

    def add(self, path, handler, methods=None, *, name=None) -> Route:
        """Register and return a route; methods defaults to GET."""

        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Route name must be a non-empty string")

            if name in self.named_routes:
                raise ValueError(f"Duplicate route name: {name}")

        route = Route(path=path, handler=handler, methods=methods, name=name)
        for earlier in self.routes:
            common = self._effective_methods(earlier) & self._effective_methods(route)
            if not common:
                continue
            if earlier.path == route.path:
                detail = "duplicate path and overlapping methods"
            elif (
                not route.parameters
                and earlier.parameters
                and earlier.pattern.fullmatch(route.path)
            ):
                detail = "earlier parameter route shadows later literal path"
            else:
                continue
            warnings.warn(
                f"Route {route.path!r} ({', '.join(sorted(common))}) is unreachable: "
                f"{detail}; earlier {earlier.path!r} takes precedence in registration order",
                UserWarning,
                stacklevel=2,
            )
        self.routes.append(route)

        if name is not None:
            self.named_routes[name] = route

        return route

    @staticmethod
    def _effective_methods(route):
        methods = set(route.methods)
        if "GET" in methods:
            methods.add("HEAD")
        return methods

    def resolve(self, request: Request) -> tuple[Route | None, dict[str, str]]:
        """Return the first matching route and parameters, or (None, {})."""

        for route in self.routes:
            params = route.matches(request)

            if params is not None:
                return route, params

        return None, {}

    def url_for(self, name, **params):
        """Generate a URL for a registered named route."""

        try:
            route = self.named_routes[name]
        except KeyError:
            raise LookupError(f"Unknown route name: {name}") from None

        return route.build_url(**params)
