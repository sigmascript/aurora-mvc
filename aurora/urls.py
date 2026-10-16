"""Named-route URL generation for Aurora applications."""

from contextvars import ContextVar

_current_application = ContextVar("aurora_current_application", default=None)


def url_for(name, **params):
    """Generate a URL inside a request handler; outside use app.url_for()."""
    app = _current_application.get()
    if app is None:
        raise RuntimeError(
            "url_for() requires an active request; use app.url_for() outside handlers"
        )
    return app.url_for(name, **params)
