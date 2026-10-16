"""Developer-facing MVC HTTP-method dispatch regression tests."""
import asyncio
import unittest

from aurora import Application, Controller, redirect, View
from aurora.core.request import Request
from aurora.core.response import RedirectResponse


class TestMethodControllers(unittest.TestCase):
    def test_get_post_and_route_parameters(self):
        class Resource(Controller):
            def get(self, id):
                return f"read:{id}"

            async def post(self, id):
                return f"write:{id}"

        app = Application()
        route = app.route("/items/{id}", Resource)
        self.assertEqual(route.methods, {"GET", "POST"})
        self.assertEqual(Resource.dispatch(Request({"method": "GET", "path": "/items/5"}), id="5"), "read:5")
        self.assertEqual(asyncio.run(Resource.dispatch(Request({"method": "POST", "path": "/items/5"}), id="5")), "write:5")

    def test_missing_http_method_returns_405(self):
        from aurora.core.http_exception import HTTPException

        class ReadOnly(Controller):
            def get(self):
                return "ok"

        with self.assertRaises(HTTPException) as caught:
            ReadOnly.dispatch(Request({"method": "DELETE", "path": "/"}))
        self.assertEqual(caught.exception.status_code, 405)

    def test_reject_non_controller_or_empty_controller(self):
        app = Application()
        with self.assertRaises(TypeError):
            app.route("/", lambda request: "hello")
        with self.assertRaises(ValueError):
            app.route("/", Controller)

    def test_redirect_defaults_to_303_and_allows_override(self):
        self.assertIsInstance(redirect("/notes"), RedirectResponse)
        self.assertEqual(redirect("/notes").status_code, 303)
        self.assertEqual(redirect("/next", 307).status_code, 307)
        self.assertEqual(redirect("/next").headers["Location"], "/next")

    def test_view_is_exported(self):
        self.assertEqual(View("home.html").name, "home.html")

    def test_method_route_unknown_method_asgi_405(self):
        class ReadOnly(Controller):
            def get(self):
                return "ok"

        app = Application()
        app.route("/item", ReadOnly)

        async def invoke(method, path):
            events = []
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}
            async def send(message):
                events.append(message)
            await app({"type": "http", "method": method, "path": path, "headers": []}, receive, send)
            return events[0]["status"]

        self.assertEqual(asyncio.run(invoke("POST", "/item")), 405)
        self.assertEqual(asyncio.run(invoke("GET", "/missing")), 404)
        self.assertEqual(asyncio.run(invoke("GET", "/item")), 200)


if __name__ == "__main__":
    unittest.main()
