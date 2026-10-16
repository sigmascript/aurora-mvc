"""Optional controller authentication guards.

Authentication is application-owned: callers must provide a predicate that
checks trustworthy request state (for example, a server-side session).
"""

import inspect
from functools import wraps

from aurora.core.http_exception import HTTPException
from aurora.core.response import redirect
from aurora.urls import _current_application, url_for
from .return_urls import append_next, safe_next


def _validate(name, authenticated, api):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("destination must be a non-empty named route")
    if not callable(authenticated):
        raise TypeError("authenticated must be a callable accepting Request")
    if not isinstance(api, bool):
        raise TypeError("api must be a boolean")


def _request(controller):
    if not hasattr(controller, "request"):
        raise TypeError("Authentication guards require a controller with a request")
    return controller.request


def _route_destination(request, route_name):
    """Resolve a named route in the active URL locale, when one exists.

    Only the authoritative URL locale is used. A preference cookie must not
    silently change the locale of a redirect from an unprefixed URL.
    """
    locale = getattr(request, "locale", None)
    if locale is None:
        return url_for(route_name)
    app = _current_application.get()
    if app is None:
        raise RuntimeError("Locale-aware redirects require an active application")
    return app.localized_url_for(route_name, language=locale)


def login_required(signin_route, *, authenticated, api=False, with_next=False):
    """Guard a controller action using an application-defined authentication check.

    Browser GET/HEAD redirects to the named sign-in route. Other methods fail
    with 403. Explicit API mode returns 401 for all methods. The `next` return
    destination is included only when with_next=True; validate it after login.
    """
    _validate(signin_route, authenticated, api)
    if not isinstance(with_next, bool):
        raise TypeError("with_next must be a boolean")

    def decorate(action):
        def denied(request):
            if api:
                raise HTTPException(401, "Authentication required")
            if request.method not in {"GET", "HEAD"}:
                raise HTTPException(403, "Authentication required")
            destination = _route_destination(request, signin_route)
            if with_next:
                original = request.path
                if request.query_string:
                    original += "?" + request.query_string
                # Invalid or ambiguous paths never reach the Location header.
                original = safe_next(original, "/")
                destination = append_next(destination, original)
            return redirect(destination)

        if inspect.iscoroutinefunction(action):
            @wraps(action)
            async def async_wrapper(self, *args, **kwargs):
                request = _request(self)
                state = authenticated(request)
                if inspect.isawaitable(state):
                    state = await state
                if not state:
                    return denied(request)
                return await action(self, *args, **kwargs)
            return async_wrapper

        @wraps(action)
        def sync_wrapper(self, *args, **kwargs):
            request = _request(self)
            state = authenticated(request)
            if inspect.isawaitable(state):
                if inspect.iscoroutine(state):
                    state.close()
                raise TypeError("Async authentication predicates require async actions")
            if not state:
                return denied(request)
            return action(self, *args, **kwargs)
        return sync_wrapper

    return decorate


def login_abort(destination_route, *, authenticated):
    """Redirect authenticated visitors away from an action (e.g. sign-in)."""
    _validate(destination_route, authenticated, False)

    def decorate(action):
        if inspect.iscoroutinefunction(action):
            @wraps(action)
            async def async_wrapper(self, *args, **kwargs):
                request = _request(self)
                state = authenticated(request)
                if inspect.isawaitable(state):
                    state = await state
                if state:
                    return redirect(_route_destination(request, destination_route))
                return await action(self, *args, **kwargs)
            return async_wrapper

        @wraps(action)
        def sync_wrapper(self, *args, **kwargs):
            request = _request(self)
            state = authenticated(request)
            if inspect.isawaitable(state):
                if inspect.iscoroutine(state):
                    state.close()
                raise TypeError("Async authentication predicates require async actions")
            if state:
                return redirect(_route_destination(request, destination_route))
            return action(self, *args, **kwargs)
        return sync_wrapper

    return decorate
