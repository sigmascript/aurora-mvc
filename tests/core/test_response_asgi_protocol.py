"""ASGI response protocol contracts for routed HTTP requests."""

import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.response import Response


def invoke(app, method="GET", path="/"):
    async def run():
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await app({"type": "http", "method": method, "path": path,
                   "headers": [], "query_string": b""}, receive, send)
        return sent

    return asyncio.run(run())


class ResponseProtocolTests(unittest.TestCase):
    def test_head_suppresses_body_but_preserves_get_length(self):
        app = Application()
        app.add_route("/", lambda request: Response("hello"), methods={"GET", "HEAD"})
        get_messages = invoke(app)
        head_messages = invoke(app, "HEAD")
        self.assertEqual(head_messages[0]["status"], 200)
        self.assertEqual(head_messages[0]["headers"], get_messages[0]["headers"])
        self.assertEqual(head_messages[-1]["body"], b"")
        self.assertEqual(get_messages[-1]["body"], b"hello")

    def test_head_suppresses_error_response_body(self):
        app = Application()
        messages = invoke(app, "HEAD", "/missing")
        self.assertEqual(messages[0]["status"], 404)
        self.assertEqual(messages[-1]["body"], b"")

    def test_204_and_304_have_no_body_or_entity_headers(self):
        for status in (204, 304):
            with self.subTest(status=status):
                app = Application()
                app.add_route("/", lambda request, code=status: Response("unexpected", status_code=code))
                messages = invoke(app)
                self.assertEqual(messages[0]["status"], status)
                self.assertEqual(messages[-1]["body"], b"")
                keys = {name for name, _ in messages[0]["headers"]}
                self.assertNotIn(b"content-length", keys)
                self.assertNotIn(b"content-type", keys)

    def test_bodyless_response_preserves_custom_headers(self):
        app = Application()
        app.add_route("/", lambda request: Response("ignored", status_code=204,
                                                    headers={"x-trace": "ok"}))
        messages = invoke(app)
        self.assertIn([b"x-trace", b"ok"], messages[0]["headers"])

    def test_head_transport_failure_is_not_retried(self):
        app = Application()
        app.add_route("/", lambda request: Response("hello"), methods={"HEAD"})

        async def run():
            calls = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                calls.append(message["type"])
                if message["type"] == "http.response.body":
                    raise RuntimeError("transport failure")

            with self.assertRaisesRegex(RuntimeError, "transport failure"):
                await app({"type": "http", "method": "HEAD", "path": "/",
                           "headers": []}, receive, send)
            return calls

        self.assertEqual(asyncio.run(run()), ["http.response.start", "http.response.body"])


if __name__ == "__main__":
    unittest.main()
