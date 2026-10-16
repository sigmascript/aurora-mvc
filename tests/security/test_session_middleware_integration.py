"""B4 integration regression tests for middleware and session lifecycle."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.request import ClientDisconnect
from aurora.core.response import Response
from aurora.security.sessions import SessionSettings


def invoke(app, *, cookie=None):
    async def run():
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
        await app({"type": "http", "method": "GET", "path": "/", "headers": headers,
                   "query_string": b""}, receive, send)
        return messages
    return asyncio.run(run())


def cookie_value(messages):
    headers = messages[0]["headers"]
    for name, value in headers:
        if name.lower() == b"set-cookie" and value.startswith(b"aurora_session="):
            return value.split(b";", 1)[0].decode("ascii")
    return None


class MiddlewareSessionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(sessions=SessionSettings(
            database_path=str(Path(self.temp.name) / "sessions.sqlite3"), secure=False))

    def test_middleware_sees_loaded_session_and_persists_changes(self):
        @self.app.add_middleware
        async def middleware(request, call_next):
            request.session["from_middleware"] = "yes"
            return await call_next(request)
        self.app.add_route("/", lambda request: Response(request.session["from_middleware"]))
        messages = invoke(self.app)
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(messages[-1]["body"], b"yes")
        cookie = cookie_value(messages)
        self.assertIsNotNone(cookie)
        self.assertEqual(invoke(self.app, cookie=cookie)[0]["status"], 200)

    def test_short_circuit_still_persists_session(self):
        @self.app.add_middleware
        async def middleware(request, call_next):
            request.session["blocked"] = True
            return Response("blocked", status_code=403)
        self.app.add_route("/", lambda request: Response("handler"))
        messages = invoke(self.app)
        self.assertEqual(messages[0]["status"], 403)
        self.assertIsNotNone(cookie_value(messages))

    def test_http_exception_does_not_persist_partial_session_change(self):
        @self.app.add_middleware
        async def middleware(request, call_next):
            request.session["temporary"] = True
            raise HTTPException(status_code=403, detail="Forbidden")
        self.app.add_route("/", lambda request: Response("handler"))
        messages = invoke(self.app)
        self.assertEqual(messages[0]["status"], 403)
        self.assertIsNone(cookie_value(messages))

    def test_disconnect_stops_session_save_and_response(self):
        @self.app.add_middleware
        async def middleware(request, call_next):
            request.session["temporary"] = True
            raise ClientDisconnect()
        self.app.add_route("/", lambda request: Response("handler"))
        self.assertEqual(invoke(self.app), [])

    def test_invalid_middleware_response_does_not_persist_session(self):
        @self.app.add_middleware
        async def middleware(request, call_next):
            request.session["temporary"] = True
            return "invalid"
        self.app.add_route("/", lambda request: Response("handler"))
        messages = invoke(self.app)
        self.assertEqual(messages[0]["status"], 500)
        self.assertIsNone(cookie_value(messages))


if __name__ == "__main__":
    unittest.main()
