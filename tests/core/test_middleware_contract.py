"""D2-C5A: preserve the established HTTP middleware contract."""

import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.response import Response


def run_request(app):
    """Run a minimal HTTP request and collect the ASGI response messages."""
    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http", "path": "/", "method": "GET",
            "headers": [], "query_string": b"",
        }
        await app(scope, receive, send)
        return messages

    return asyncio.run(run())


class MiddlewareContractTests(unittest.TestCase):
    def test_call_next_rejects_second_invocation(self):
        app = Application()
        calls = []

        @app.add_middleware
        async def middleware(request, call_next):
            response = await call_next(request)
            with self.assertRaisesRegex(RuntimeError, "only be called once"):
                await call_next(request)
            return response

        def handler(request):
            calls.append("handler")
            return Response("ok")

        app.add_route("/", handler)
        self.assertEqual(run_request(app)[0]["status"], 200)
        self.assertEqual(calls, ["handler"])

    def test_call_next_rejects_different_request(self):
        app = Application()
        calls = []

        @app.add_middleware
        async def middleware(request, call_next):
            with self.assertRaisesRegex(ValueError, "original request"):
                await call_next(object())
            return await call_next(request)

        app.add_route("/", lambda request: calls.append("handler") or Response("ok"))
        self.assertEqual(run_request(app)[0]["status"], 200)
        self.assertEqual(calls, ["handler"])

    def test_invalid_result_after_downstream_becomes_500(self):
        app = Application()
        calls = []

        @app.add_middleware
        async def middleware(request, call_next):
            await call_next(request)
            return "not a response"

        app.add_route("/", lambda request: calls.append("handler") or Response("ok"))
        messages = run_request(app)
        self.assertEqual(calls, ["handler"])
        self.assertEqual(messages[0]["status"], 500)
        self.assertEqual(messages[-1]["body"], b"Internal Server Error")

    def test_application_error_response_bypasses_middleware_postprocessing(self):
        app = Application()
        events = []

        @app.add_middleware
        async def middleware(request, call_next):
            events.append("before")
            response = await call_next(request)
            events.append("after")
            response.headers["x-middleware"] = "yes"
            return response

        def handler(request):
            raise HTTPException(status_code=403, detail="Forbidden")

        app.add_route("/", handler)
        messages = run_request(app)
        self.assertEqual(messages[0]["status"], 403)
        self.assertEqual(events, ["before"])
        self.assertNotIn([b"x-middleware", b"yes"], messages[0]["headers"])
