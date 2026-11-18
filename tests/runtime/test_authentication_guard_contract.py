"""Controller guards preserve browser, API and return-URL boundaries."""

import asyncio

import pytest

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.security import login_abort, login_required, request_next, safe_next
from aurora.urls import _current_application


def _request(method="GET", authenticated=False, query=b""):
    return Request({
        "type": "http",
        "method": method,
        "path": "/private",
        "query_string": query,
        "authenticated": authenticated,
    })


@pytest.fixture
def routes():
    app = Application()
    app.add_route("/signin", lambda request: "signin", name="auth.signin")
    app.add_route("/notes", lambda request: "notes", name="notes.index")
    token = _current_application.set(app)
    try:
        yield
    finally:
        _current_application.reset(token)


def test_browser_redirects_and_authorized_action(routes):
    class Private(Controller):
        @login_required("auth.signin", authenticated=lambda request: request.scope["authenticated"], with_next=True)
        def get(self):
            return "private"

    response = Private(_request(query=b"page=2")).get()
    assert response.status_code == 303
    assert response.headers["Location"] == "/signin?next=%2Fprivate%3Fpage%3D2"
    assert Private(_request(authenticated=True)).get() == "private"


def test_mutating_methods_and_api_fail_closed(routes):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        class Private(Controller):
            @login_required("auth.signin", authenticated=lambda request: False)
            def post(self):
                return "private"

        with pytest.raises(HTTPException) as rejected:
            Private(_request(method)).post()
        assert rejected.value.status_code == 403

    class Api(Controller):
        @login_required("auth.signin", authenticated=lambda request: False, api=True)
        def get(self):
            return "private"

    with pytest.raises(HTTPException) as rejected:
        Api(_request()).get()
    assert rejected.value.status_code == 401


def test_async_authentication_and_signin_redirect(routes):
    async def authenticated(request):
        return request.scope["authenticated"]

    class Private(Controller):
        @login_required("auth.signin", authenticated=authenticated)
        async def get(self):
            return "private"

    class Signin(Controller):
        @login_abort("notes.index", authenticated=authenticated)
        async def get(self):
            return "signin"

    assert asyncio.run(Private(_request(authenticated=True)).get()) == "private"
    assert asyncio.run(Private(_request()).get()).headers["Location"] == "/signin"
    assert asyncio.run(Signin(_request(authenticated=True)).get()).headers["Location"] == "/notes"


def test_return_destination_never_accepts_external_or_ambiguous_urls():
    for candidate in ("/%25252f%25252fevil.test", "/%250aevil", "/private\x00bad", "/private\r\nLocation:evil"):
        assert safe_next(candidate, "/safe") == "/safe"

    for query in (b"", b"next=", b"next=%2Fone&next=%2Ftwo", b"next=https%3A%2F%2Fevil.test"):
        assert request_next(_request(query=query), "/safe") == "/safe"
