"""Pin response preparation and ASGI transmission compatibility contracts."""

import unittest

from aurora.core.application import Application
from aurora.core.response import Response


class CountingResponse(Response):
    """Observe preparation without changing its serialization behavior."""

    def __init__(self, content="hello"):
        super().__init__(content)
        self.prepare_calls = 0

    def prepare(self):
        self.prepare_calls += 1
        return super().prepare()


class CustomSenderResponse(Response):
    """A custom sender must remain supported by the application boundary."""

    def __init__(self):
        super().__init__("hello")
        self.send_calls = 0

    async def __call__(self, send):
        self.send_calls += 1
        await super().__call__(send)


class ResponsePreparationContractTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, response, method="GET", send_hook=None):
        app = Application()
        app.add_route("/", lambda request: response, methods={"GET", "HEAD"})
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)
            if send_hook is not None:
                await send_hook(message)

        await app(
            {"type": "http", "path": "/", "method": method, "headers": []},
            receive,
            send,
        )
        return messages

    async def test_current_standard_response_prepares_twice(self):
        response = CountingResponse()
        messages = await self.invoke(response)
        self.assertEqual(response.prepare_calls, 2)
        self.assertEqual([m["type"] for m in messages], [
            "http.response.start", "http.response.body"
        ])
        self.assertEqual(messages[-1]["body"], b"hello")

    async def test_head_preserves_custom_sender_and_suppresses_body(self):
        response = CustomSenderResponse()
        messages = await self.invoke(response, method="HEAD")
        self.assertEqual(response.send_calls, 1)
        self.assertEqual(messages[0]["status"], 200)
        self.assertIn([b"content-length", b"5"], messages[0]["headers"])
        self.assertEqual(messages[-1]["body"], b"")

    async def test_invalid_preparation_is_converted_before_send(self):
        response = Response("ok", headers={"x-invalid": "bad\r\nvalue"})
        messages = await self.invoke(response)
        self.assertEqual(messages[0]["status"], 500)
        self.assertEqual(messages[-1]["body"], b"Internal Server Error")

    async def test_transport_failure_is_not_retried(self):
        calls = []

        async def fail_on_body(message):
            calls.append(message["type"])
            if message["type"] == "http.response.body":
                raise OSError("transport closed")

        with self.assertRaisesRegex(OSError, "transport closed"):
            await self.invoke(CountingResponse(), send_hook=fail_on_body)
        self.assertEqual(calls, ["http.response.start", "http.response.body"])


if __name__ == "__main__":
    unittest.main()
