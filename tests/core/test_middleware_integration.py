"""B4 minimal HTTP middleware boundary regression tests."""
import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.response import Response


def run_request(app, path="/", method="GET"):
    async def run():
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        await app({"type": "http", "path": path, "method": method, "headers": [], "query_string": b""}, receive, send)
        return sent
    return asyncio.run(run())


class MiddlewareTests(unittest.TestCase):
    def test_order_and_response_headers(self):
        app = Application()
        events = []
        @app.add_middleware
        async def first(request, call_next):
            events.append("first-before")
            response = await call_next(request)
            events.append("first-after")
            response.headers["x-first"] = "yes"
            return response
        @app.add_middleware
        async def second(request, call_next):
            events.append("second-before")
            response = await call_next(request)
            events.append("second-after")
            return response
        async def handler(request):
            events.append("handler")
            return Response("ok")
        app.add_route("/", handler)
        messages = run_request(app)
        self.assertEqual(events, ["first-before", "second-before", "handler", "second-after", "first-after"])
        self.assertIn([b"x-first", b"yes"], messages[0]["headers"])

    def test_short_circuit(self):
        app = Application()
        called = []
        @app.add_middleware
        async def block(request, call_next):
            return Response("blocked", status_code=403)
        async def handler(request):
            called.append(True)
            return Response("should not run")
        app.add_route("/", handler)
        self.assertEqual(run_request(app)[0]["status"], 403)
        self.assertFalse(called)

    def test_rejects_non_async_registration(self):
        app = Application()
        with self.assertRaises(TypeError):
            app.add_middleware(lambda request, call_next: None)

    def test_invalid_middleware_result_returns_500(self):
        app = Application()
        @app.add_middleware
        async def bad(request, call_next):
            return "not a response"
        app.add_route("/", lambda request: Response("ok"))
        self.assertEqual(run_request(app)[0]["status"], 500)

    def test_http_errors_follow_existing_application_boundary(self):
        from aurora.core.http_exception import HTTPException
        app = Application()
        @app.add_middleware
        async def deny(request, call_next):
            raise HTTPException(status_code=401, detail="Unauthorized")
        app.add_route("/", lambda request: Response("ok"))
        self.assertEqual(run_request(app)[0]["status"], 401)

    def test_no_middleware_preserves_original_behavior(self):
        app = Application()
        app.add_route("/", lambda request: Response("original"))
        messages = run_request(app)
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(messages[-1]["body"], b"original")


if __name__ == "__main__":
    unittest.main()
