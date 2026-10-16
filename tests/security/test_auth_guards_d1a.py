"""D1-A: independent guard tests; no Free Notes imports."""

import asyncio
import inspect
import unittest

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.security import login_abort, login_required
from aurora.urls import _current_application


def authenticated(request):
    return request.scope.get("authenticated", False)


class AuthGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        self.app.add_route("/signin", lambda request: "sign in", name="auth.signin")
        self.app.add_route("/notes", lambda request: "notes", name="notes.index")
        self.token = _current_application.set(self.app)

    def tearDown(self):
        _current_application.reset(self.token)

    def request(self, method="GET", logged_in=False):
        return Request({"type": "http", "method": method, "path": "/private", "authenticated": logged_in})

    def test_required_sync_redirects_unauthenticated_get(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated)
            def get(self):
                return "secret"
        result = Private(self.request()).get()
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["Location"], "/signin")
        self.assertEqual(Private(self.request(logged_in=True)).get(), "secret")
        self.assertFalse(inspect.iscoroutinefunction(Private.get))

    def test_required_post_is_forbidden(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated)
            def post(self):
                return "secret"
        with self.assertRaises(HTTPException) as error:
            Private(self.request("POST")).post()
        self.assertEqual(error.exception.status_code, 403)

    def test_required_api_mode_is_401(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated, api=True)
            def get(self):
                return "secret"
        with self.assertRaises(HTTPException) as error:
            Private(self.request()).get()
        self.assertEqual(error.exception.status_code, 401)

    def test_abort_sync(self):
        class Signin(Controller):
            @login_abort("notes.index", authenticated=authenticated)
            def get(self):
                return "signin"
        self.assertEqual(Signin(self.request()).get(), "signin")
        result = Signin(self.request(logged_in=True)).get()
        self.assertEqual(result.headers["Location"], "/notes")

    def test_async_actions_and_predicates(self):
        async def check(request):
            return authenticated(request)

        class Private(Controller):
            @login_required("auth.signin", authenticated=check)
            async def get(self):
                return "secret"

        class Signin(Controller):
            @login_abort("notes.index", authenticated=check)
            async def get(self):
                return "signin"

        self.assertEqual(asyncio.run(Private(self.request("GET", True)).get()), "secret")
        self.assertEqual(asyncio.run(Private(self.request()).get()).headers["Location"], "/signin")
        self.assertEqual(asyncio.run(Signin(self.request("GET", True)).get()).headers["Location"], "/notes")

    def test_invalid_configuration(self):
        with self.assertRaises(TypeError):
            login_required("auth.signin", authenticated=None)
        with self.assertRaises(ValueError):
            login_abort("", authenticated=authenticated)


if __name__ == "__main__":
    unittest.main()
