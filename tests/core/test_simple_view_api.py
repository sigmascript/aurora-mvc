"""Regression coverage for simple View responses and shared view context."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora import Application, View
from aurora.urls import url_for


def request_app(app, path="/"):
    async def run():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await app({"type": "http", "method": "GET", "path": path,
                   "headers": [], "query_string": b""}, receive, send)
        return messages

    messages = asyncio.run(run())
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = next(m for m in messages if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body["body"].decode()


class SimpleViewAPITests(unittest.TestCase):
    def test_view_keywords_and_metadata(self):
        page = View("auth/signin", title="Sign in", status_code=400,
                    headers={"Cache-Control": "no-store"})
        self.assertEqual(page.name, "auth/signin")
        self.assertEqual(page.context, {"title": "Sign in"})
        self.assertEqual(page.status_code, 400)
        self.assertEqual(page.headers["Cache-Control"], "no-store")

    def test_render_extensionless_name_with_request_context(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "signin.html").write_text(
                "{{ title }} / {{ csrf_token }}", encoding="utf-8")
            app = Application(views_directory=directory,
                              view_context=lambda request: {"csrf_token": "abc"},
                              view_headers={"Cache-Control": "no-store"})
            app.add_route("/", lambda request: View("signin", title="Hello"))
            status, headers, body = request_app(app)
            self.assertEqual(status, 200)
            self.assertEqual(body, "Hello / abc")
            self.assertEqual(headers[b"cache-control"], b"no-store")

    def test_explicit_values_override_shared_context(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "page.html").write_text("{{ title }}", encoding="utf-8")
            app = Application(views_directory=directory,
                              view_context=lambda request: {"title": "Shared"})
            app.add_route("/", lambda request: View("page", title="Explicit"))
            self.assertEqual(request_app(app)[2], "Explicit")

    def test_view_status_and_custom_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "error.html").write_text("Invalid", encoding="utf-8")
            app = Application(views_directory=directory)
            app.add_route("/", lambda request: View("error", status_code=400,
                        headers={"X-Example": "yes"}))
            status, headers, body = request_app(app)
            self.assertEqual(status, 400)
            self.assertEqual(headers[b"x-example"], b"yes")
            self.assertEqual(body, "Invalid")

    def test_url_for_available_in_view_context_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "page.html").write_text("{{ destination }}", encoding="utf-8")
            app = Application(
                views_directory=directory,
                view_context=lambda request: {"destination": url_for("destination")},
            )
            app.add_route("/", lambda request: View("page"))
            app.add_route("/destination", lambda request: "OK", name="destination")
            status, _, body = request_app(app)
            self.assertEqual(status, 200)
            self.assertEqual(body, "/destination")
            with self.assertRaises(RuntimeError):
                url_for("destination")

    def test_context_provider_validation(self):
        with self.assertRaises(TypeError):
            Application(view_context="not callable")
