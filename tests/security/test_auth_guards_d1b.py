"""D1-B return URL safety and guard integration tests."""
import unittest

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.request import Request
from aurora.security import login_required, request_next, safe_next
from aurora.urls import _current_application


def logged_in(request):
    return request.scope.get("logged_in", False)


class ReturnUrlTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        self.app.add_route("/signin", lambda request: "signin", name="auth.signin")
        self.token = _current_application.set(self.app)

    def tearDown(self):
        _current_application.reset(self.token)

    def request(self, path="/notes/new", query=b"", method="GET", logged_in=False):
        return Request({"type": "http", "path": path, "method": method,
                        "query_string": query, "logged_in": logged_in})

    def test_local_destinations(self):
        for value in ("/notes/new", "/en/notes/new", "/notes?page=2", "/hello%20world"):
            with self.subTest(value=value):
                self.assertEqual(safe_next(value, "/notes"), value)

    def test_rejects_unsafe_destinations(self):
        unsafe = ("https://evil.example/", "//evil.example/", "/%2f%2fevil.example/",
                  "/%252f%252fevil.example/", "\\evil.example", "/%5c\\evil",
                  "/%00bad", "/%250dbad", "/notes#fragment", "notes/new", "")
        for value in unsafe:
            with self.subTest(value=value):
                self.assertEqual(safe_next(value, "/notes"), "/notes")

    def test_invalid_fallback_is_error(self):
        with self.assertRaises(ValueError):
            safe_next("/safe", "https://evil.example")

    def test_request_next_duplicate_and_malformed(self):
        self.assertEqual(request_next(self.request(query=b"next=%2Fnotes%2Fnew"), "/"), "/notes/new")
        self.assertEqual(request_next(self.request(query=b"next=%2Fgood&next=%2Fbad"), "/"), "/")
        self.assertEqual(request_next(self.request(query=b"next=https%3A%2F%2Fevil.example"), "/"), "/")

    def test_guard_adds_next_for_get(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=logged_in, with_next=True)
            def get(self):
                return "secret"
        result = Private(self.request(query=b"page=2&sort=new")).get()
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers["Location"],
                         "/signin?next=%2Fnotes%2Fnew%3Fpage%3D2%26sort%3Dnew")
        self.assertEqual(Private(self.request(logged_in=True)).get(), "secret")

    def test_guard_ignores_unsafe_request_path(self):
        class Private(Controller):
            @login_required("auth.signin", authenticated=logged_in, with_next=True)
            def get(self):
                return "secret"
        result = Private(self.request(path="//evil.example")).get()
        self.assertEqual(result.headers["Location"], "/signin?next=%2F")

    def test_opt_in_and_post_unchanged(self):
        from aurora.core.http_exception import HTTPException
        class Private(Controller):
            @login_required("auth.signin", authenticated=logged_in)
            def get(self):
                return "secret"
        self.assertEqual(Private(self.request()).get().headers["Location"], "/signin")
        class Post(Controller):
            @login_required("auth.signin", authenticated=logged_in, with_next=True)
            def post(self):
                return "secret"
        with self.assertRaises(HTTPException) as ctx:
            Post(self.request(method="POST")).post()
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
