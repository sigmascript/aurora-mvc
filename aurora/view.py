"""Simple, declarative HTML views for Aurora applications."""


class View:
    """A view to render, with context and optional HTTP response metadata.

    Examples:
        View("home", title="Welcome")
        View("auth/signin", error="Invalid login", status_code=400)

    A dictionary as the second positional argument remains supported.
    ``status_code`` and ``headers`` control the eventual HTTP response.
    """

    def __init__(
        self, template: str, context=None, *, status_code=200, headers=None, **values
    ):
        if context is not None and not isinstance(context, dict):
            raise TypeError("View context must be a dictionary")
        self.name = template
        self.context = dict(context or {})
        self.context.update(values)
        self.status_code = status_code
        self.headers = dict(headers or {})


def view(view_file: str, **context) -> View:
    """Create a view using keyword arguments for template context."""
    return View(view_file, **context)
