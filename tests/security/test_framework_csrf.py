import unittest

from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.core.session import Session
from aurora.security import csrf_token, validate_csrf, protected_form


def request(method="POST", data=None):
    raw = (data or "").encode()
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    req = Request({"type": "http", "method": method, "path": "/", "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}, receive)
    req._session = Session()
    return req


class TestFrameworkCSRF(unittest.IsolatedAsyncioTestCase):
    def test_token_created_lazily_and_stable(self):
        req = request()
        self.assertFalse(req.session.changed)
        token = csrf_token(req)
        self.assertTrue(req.session.changed)
        self.assertEqual(token, csrf_token(req))
        self.assertGreaterEqual(len(token), 32)

    def test_valid_and_invalid_tokens(self):
        req = request()
        token = csrf_token(req)
        self.assertTrue(validate_csrf(req, token))
        for candidate in (None, "", "incorrect", 123, "a" * 300, "é"):
            with self.subTest(candidate=candidate), self.assertRaises(HTTPException) as caught:
                validate_csrf(req, candidate)
            self.assertEqual(caught.exception.status_code, 403)

    def test_validation_does_not_create_session(self):
        req = request()
        with self.assertRaises(HTTPException):
            validate_csrf(req, "some-token")
        self.assertFalse(req.session.changed)

    def test_other_session_cannot_reuse_token(self):
        token = csrf_token(request())
        second = request()
        csrf_token(second)
        with self.assertRaises(HTTPException):
            validate_csrf(second, token)

    async def test_regeneration_rotates_csrf_token(self):
        req = request()
        old = csrf_token(req)
        await req.session.regenerate()
        new = csrf_token(req)
        self.assertNotEqual(old, new)
        with self.assertRaises(HTTPException):
            validate_csrf(req, old)
        self.assertTrue(validate_csrf(req, new))

    async def test_protected_form(self):
        req = request()
        token = csrf_token(req)
        req = request(data=f"csrf_token={token}&title=hello")
        req._session = Session({"_aurora_csrf_token": token})
        data = await protected_form(req)
        self.assertEqual(data["title"], "hello")
        bad = request(data="title=hello")
        bad._session = Session({"_aurora_csrf_token": token})
        with self.assertRaises(HTTPException) as caught:
            await protected_form(bad)
        self.assertEqual(caught.exception.status_code, 403)

    def test_safe_method_rejected(self):
        req = request("GET")
        token = csrf_token(req)
        with self.assertRaises(HTTPException) as caught:
            validate_csrf(req, token)
        self.assertEqual(caught.exception.status_code, 405)
