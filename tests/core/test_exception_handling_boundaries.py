"""Regression contracts for application-level ASGI exception boundaries."""

import unittest

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.response import Response


class ExceptionBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke(self, app, *, send=None):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def capture(message):
            messages.append(message)
            if send is not None:
                await send(message)

        await app({"type": "http", "method": "GET", "path": "/"}, receive, capture)
        return messages

    async def test_middleware_http_exception_uses_intended_status(self):
        app = Application()
        app.add_route("/", lambda request: "ok")

        @app.add_middleware
        async def reject(request, call_next):
            raise HTTPException(403, "Denied")

        messages = await self._invoke(app)
        self.assertEqual(messages[0]["status"], 403)
        self.assertEqual(messages[1]["body"], b"Denied")

    async def test_middleware_unexpected_exception_is_sanitized(self):
        app = Application()
        app.add_route("/", lambda request: "ok")

        @app.add_middleware
        async def broken(request, call_next):
            raise RuntimeError("private internal error")

        messages = await self._invoke(app)
        self.assertEqual(messages[0]["status"], 500)
        self.assertEqual(messages[1]["body"], b"Internal Server Error")

    async def test_response_preparation_failure_becomes_single_500(self):
        app = Application()

        class BrokenResponse(Response):
            def prepare(self):
                raise ValueError("private serialization details")

        app.add_route("/", lambda request: BrokenResponse("secret"))
        messages = await self._invoke(app)
        self.assertEqual([message["type"] for message in messages], [
            "http.response.start", "http.response.body"
        ])
        self.assertEqual(messages[0]["status"], 500)
        self.assertEqual(messages[1]["body"], b"Internal Server Error")

    async def test_transport_failure_is_not_retried_as_http_response(self):
        app = Application()
        app.add_route("/", lambda request: Response("ok"))
        attempts = []

        async def fail_after_headers(message):
            attempts.append(message["type"])
            if message["type"] == "http.response.body":
                raise OSError("connection lost")

        with self.assertRaises(OSError):
            await self._invoke(app, send=fail_after_headers)
        self.assertEqual(attempts, ["http.response.start", "http.response.body"])
