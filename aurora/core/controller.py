from aurora.core.request import Request


class Controller:
    """Base class for request-bound controller actions.

    Each dispatched request creates a controller instance with ``self.request``.
    """

    def __init__(self, request: Request):
        self.request = request

    @classmethod
    def action(cls, action_name: str):
        """Create a route handler for a named instance method.

        Register it with app.add_route(path, MyController.action("show")).
        Each call creates a controller and passes route parameters to the
        method as keyword arguments. Async results are awaited by Application.
        """
        if not isinstance(action_name, str) or not action_name.strip():
            raise ValueError("Action name must be a non-empty string")

        def handler(request: Request, **params):
            controller = cls(request)
            action = getattr(controller, action_name)
            return action(**params)

        return handler

    @classmethod
    def dispatch(cls, request: Request, **params):
        """Dispatch an HTTP request to the matching method on a new controller.

        Methods may be synchronous or asynchronous; Application awaits results.
        Unsupported methods return 405 rather than silently falling through.
        """
        from aurora.core.http_exception import HTTPException

        method = request.method.lower()
        if method not in {"get", "post", "put", "patch", "delete", "head", "options"}:
            raise HTTPException(405, "Method Not Allowed")
        action = getattr(cls(request), method, None)
        if not callable(action):
            raise HTTPException(405, "Method Not Allowed")
        return action(**params)

    @classmethod
    def http_methods(cls):
        """Return callable HTTP verbs, including those inherited from parents."""
        allowed = ("get", "post", "put", "patch", "delete", "head", "options")
        return [name.upper() for name in allowed if callable(getattr(cls, name, None))]
