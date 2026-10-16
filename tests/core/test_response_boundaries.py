"""ASGI response preparation and transmission boundary regressions."""

import unittest

from aurora.core.application import Application
from aurora.core.response import Response


class ResponseBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, app, send=None):
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def collect(message):
            messages.append(message)
            if send is not None:
                await send(message)

        await app(
            {"type": "http", "path": "/", "method": "GET", "headers": []},
            receive,
            collect,
        )
        return messages

    async def test_invalid_header_yields_one_safe_500(self):
        app = Application()
        app.add_route("/", lambda request: Response("secret", headers={"x-bad": "a\r\nb"}))
        messages = await self.invoke(app)
        self.assertEqual([message["type"] for message in messages], [
            "http.response.start", "http.response.body"
        ])
        self.assertEqual(messages[0]["status"], 500)
        self.assertEqual(messages[1]["body"], b"Internal Server Error")

    async def test_invalid_body_encoding_yields_500(self):
        app = Application()
        app.add_route("/", lambda request: Response("\ud800"))
        messages = await self.invoke(app)
        self.assertEqual(messages[0]["status"], 500)

    async def test_invalid_middleware_header_yields_500(self):
        app = Application()

        @app.add_middleware
        async def corrupt(request, call_next):
            response = await call_next(request)
            response.headers["bad header"] = "value"
            return response

        app.add_route("/", lambda request: Response("ok"))
        messages = await self.invoke(app)
        self.assertEqual(messages[0]["status"], 500)

    async def test_transport_failure_is_not_retried(self):
        app = Application()
        app.add_route("/", lambda request: Response("ok"))
        calls = []

        async def broken_send(message):
            calls.append(message["type"])
            raise OSError("connection lost")

        with self.assertRaises(OSError):
            await self.invoke(app, broken_send)
        self.assertEqual(calls, ["http.response.start"])

    async def test_body_transport_failure_does_not_send_second_start(self):
        app = Application()
        app.add_route("/", lambda request: Response("ok"))
        calls = []

        async def broken_send(message):
            calls.append(message["type"])
            if message["type"] == "http.response.body":
                raise OSError("connection lost")

        with self.assertRaises(OSError):
            await self.invoke(app, broken_send)
        self.assertEqual(calls, ["http.response.start", "http.response.body"])


if __name__ == "__main__":
    unittest.main()
