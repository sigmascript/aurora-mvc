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
        self.routes.append(route)
        if name is not None:
            self.named_routes[name] = route
        return route

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
