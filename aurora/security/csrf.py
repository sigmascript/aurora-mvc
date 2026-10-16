"""Session-bound CSRF protection for browser forms.

Call protected_form(request) before state-changing application work.
CSRF does not replace authentication, authorization, or origin checks.
"""

import secrets

from aurora.core.http_exception import HTTPException

_CSRF_KEY = "_aurora_csrf_token"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def csrf_token(request):
    """Return a session-bound form token, creating it if necessary.

    Requires an application configured with sessions=True. Include the value
    in a hidden form field named csrf_token. Rendering the form commits the
    newly generated token as part of normal session persistence.
    """
    session = request.session
    token = session.get(_CSRF_KEY)
    if token is None:
        token = secrets.token_urlsafe(32)
        session[_CSRF_KEY] = token
    return token


def validate_csrf(request, token):
    """Reject missing, malformed, or non-matching form tokens with HTTP 403.

    Only for state-changing HTTP methods. The expected token must already
    exist in the current server-side session; validation never creates one.
    """
    if request.method not in _UNSAFE_METHODS:
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    expected = request.session.get(_CSRF_KEY)
    if (
        not isinstance(expected, str)
        or not isinstance(token, str)
        or len(token) > 256
        or not token.isascii()
        or not secrets.compare_digest(expected, token)
    ):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return True


async def protected_form(request):
    """Read URL-encoded form data and validate its csrf_token field."""
    data = await request.form()
    validate_csrf(request, data.get("csrf_token"))
    return data


def csrf_field(request):
    """Return a safe HTML hidden field containing the session CSRF token.

    The result is a Markup object suitable for Jinja autoescaped templates.
    Requires sessions, just like ``csrf_token``.
    """
    from markupsafe import Markup, escape

    return (
        Markup('<input type="hidden" name="csrf_token" value="')
        + escape(csrf_token(request))
        + Markup('">')
    )
