"""ASGI disconnect regression coverage for Checkpoint B1."""
import unittest

from aurora.core.application import Application
from aurora.core.request import ClientDisconnect, Request


class RequestDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_before_body_raises(self):
        async def receive():
            return {"type": "http.disconnect"}

        with self.assertRaises(ClientDisconnect):
            await Request({"type": "http", "method": "POST"}, receive).body()

    async def test_disconnect_between_chunks_raises(self):
        messages = [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ]

        async def receive():
            return messages.pop(0)

        request = Request({"type": "http", "method": "POST"}, receive)
        with self.assertRaises(ClientDisconnect):
            await request.body()
        self.assertIsNone(request._body)

    async def test_unexpected_message_rejected(self):
        async def receive():
            return {"type": "lifespan.startup"}

        with self.assertRaises(RuntimeError):
            await Request({"type": "http"}, receive).body()

    async def test_application_does_not_send_after_disconnect(self):
        app = Application()

        async def handler(request):
            await request.body()
            return "unreachable"

        app.add_route("/upload", handler, methods=["POST"])
        sent = []

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        await app({"type": "http", "path": "/upload", "method": "POST", "headers": []}, receive, send)
        self.assertEqual(sent, [])
