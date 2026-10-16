"""B4 request/error boundary regressions; no external databases."""
import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.request import ClientDisconnect
from aurora.core.response import Response


def invoke(app, *, send=None):
    async def run():
        events = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def collect(message):
            events.append(message)
            if send is not None:
                await send(message)
        await app({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b""}, receive, collect)
        return events
    return asyncio.run(run())


class MiddlewareBoundaryTests(unittest.TestCase):
    def test_call_next_is_single_use(self):
        app = Application()
        calls = []
        @app.add_middleware
        async def duplicate(request, call_next):
            await call_next(request)
            await call_next(request)
            return Response("unreachable")
        def handler(request):
            calls.append(1)
            return Response("ok")
        app.add_route("/", handler)
        self.assertEqual(invoke(app)[0]["status"], 500)
        self.assertEqual(calls, [1])

    def test_http_error_from_inner_middleware_can_be_handled_by_outer(self):
        app = Application()
        @app.add_middleware
        async def outer(request, call_next):
            try:
                return await call_next(request)
            except HTTPException:
                return Response("handled", status_code=418)
        @app.add_middleware
        async def inner(request, call_next):
            raise HTTPException(status_code=403, detail="no")
        app.add_route("/", lambda request: Response("ok"))
        messages = invoke(app)
        self.assertEqual(messages[0]["status"], 418)
        self.assertEqual(messages[-1]["body"], b"handled")

    def test_unhandled_http_error_is_mapped_at_application_boundary(self):
        app = Application()
        @app.add_middleware
        async def reject(request, call_next):
            raise HTTPException(status_code=429, detail="Slow down")
        app.add_route("/", lambda request: Response("ok"))
        self.assertEqual(invoke(app)[0]["status"], 429)

    def test_disconnect_does_not_send_response(self):
        app = Application()
        @app.add_middleware
        async def disconnect(request, call_next):
            raise ClientDisconnect()
        app.add_route("/", lambda request: Response("ok"))
        self.assertEqual(invoke(app), [])

    def test_send_failure_propagates_without_second_response(self):
        app = Application()
        app.add_route("/", lambda request: Response("ok"))
        sent = []
        async def broken_send(message):
            sent.append(message["type"])
            raise OSError("socket closed")
        with self.assertRaisesRegex(OSError, "socket closed"):
            invoke(app, send=broken_send)
        self.assertEqual(sent, ["http.response.start"])

    def test_cancelled_middleware_propagates(self):
        app = Application()
        @app.add_middleware
        async def cancelled(request, call_next):
            raise asyncio.CancelledError()
        app.add_route("/", lambda request: Response("ok"))
        with self.assertRaises(asyncio.CancelledError):
            invoke(app)


if __name__ == "__main__":
    unittest.main()
