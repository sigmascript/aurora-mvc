"""D1-C: URL-authoritative locale-aware authentication redirects."""
import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.request import Request
from aurora.security import login_abort, login_required
from aurora.urls import _current_application


def authenticated(request):
    return request.scope.get("authenticated", False)


class LocaleAuthGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = Application(locales=("en", "ku", "ar"), default_locale="en")
        self.app.add_route("/signin", lambda request: "signin", name="auth.signin")
        self.app.add_route("/notes", lambda request: "notes", name="notes.index")
        self.token = _current_application.set(self.app)

    def tearDown(self):
        _current_application.reset(self.token)

    def request(self, locale="ku", path="/ku/notes/new", logged_in=False, method="GET", query=b""):
        req = Request({"type": "http", "path": path, "method": method,
                       "query_string": query, "authenticated": logged_in})
        req.locale = locale
        return req

    def test_required_preserves_kurdish_locale_and_next(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated, with_next=True)
            def get(self):
                return "secret"
        result = Private(self.request(query=b"page=2")).get()
        self.assertEqual(result.headers["Location"], "/ku/signin?next=%2Fku%2Fnotes%2Fnew%3Fpage%3D2")

    def test_all_supported_locales(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated)
            def get(self):
                return "secret"
        for locale in ("en", "ku", "ar"):
            with self.subTest(locale=locale):
                result = Private(self.request(locale=locale, path=f"/{locale}/notes/new")).get()
                self.assertEqual(result.headers["Location"], f"/{locale}/signin")

    def test_unprefixed_url_does_not_use_preference(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated)
            def get(self):
                return "secret"
        req = self.request(locale=None, path="/notes/new")
        req.preferred_locale = "ku"
        self.assertEqual(Private(req).get().headers["Location"], "/signin")

    def test_abort_redirects_in_active_locale(self):
        class Signin(Controller):
            @login_abort("notes.index", authenticated=authenticated)
            def get(self):
                return "signin"
        self.assertEqual(Signin(self.request(logged_in=True)).get().headers["Location"], "/ku/notes")
        self.assertEqual(Signin(self.request(logged_in=False)).get(), "signin")

    def test_async_required_and_abort(self):
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
        self.assertEqual(asyncio.run(Private(self.request()).get()).headers["Location"], "/ku/signin")
        self.assertEqual(asyncio.run(Signin(self.request(logged_in=True)).get()).headers["Location"], "/ku/notes")

    def test_unsupported_locale_fails_closed(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=authenticated)
            def get(self):
                return "secret"
        with self.assertRaises(ValueError):
            Private(self.request(locale="xx")).get()


if __name__ == "__main__":
    unittest.main()
