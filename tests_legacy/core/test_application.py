import tempfile
import unittest
from pathlib import Path

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.http_exception import HTTPException
from aurora.core.response import Response
from aurora.view import view


class TestApplication(unittest.IsolatedAsyncioTestCase):

    async def test_handler_string_becomes_response(self):
        app = Application()

        def home(request):
            return "Hello Aurora"

        app.add_route("/", home)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            200,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Hello Aurora",
        )

    async def test_handler_can_return_response(self):
        app = Application()

        def created(request):
            return Response(
                "Created",
                status_code=201,
            )

        app.add_route(
            "/created",
            created,
            methods=["POST"],
        )

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/created",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            201,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Created",
        )

    async def test_not_found_response(self):
        app = Application()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/missing",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            404,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Not Found",
        )

    async def test_async_handler(self):
        app = Application()

        async def home(request):
            return "Hello Async Aurora"

        app.add_route("/", home)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            200,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Hello Async Aurora",
        )

    async def test_async_controller_action(self):
        app = Application()

        class UserController(Controller):

            async def show(self, id):
                return f"Async User: {id}"

        app.add_route(
            "/users/{id}",
            UserController.action("show"),
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/users/42",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            200,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Async User: 42",
        )

    async def test_handler_exception_becomes_500_response(self):
        app = Application()

        def broken(request):
            raise ValueError(
                "This should never be shown to the client"
            )

        app.add_route(
            "/broken",
            broken,
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/broken",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            500,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Internal Server Error",
        )

    async def test_http_exception_becomes_http_response(self):
        app = Application()

        def missing_user(request):
            raise HTTPException(
                status_code=404,
                detail="User not found",
            )

        app.add_route(
            "/users/42",
            missing_user,
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/users/42",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(scope, receive, send)

        self.assertEqual(
            messages[0]["status"],
            404,
        )

        self.assertEqual(
            messages[1]["body"],
            b"User not found",
        )

    async def test_handler_can_return_view(self):
        with tempfile.TemporaryDirectory() as directory:
            template_path = (
                Path(directory) / "home.html"
            )

            template_path.write_text(
                "<h1>{{ title }}</h1>",
                encoding="utf-8",
            )

            app = Application(
                views_directory=directory,
            )

            def home(request):
                return view(
                    "home.html",
                    title="Aurora Next",
                )

            app.add_route("/", home)

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/",
            }

            messages = []

            async def receive():
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }

            async def send(message):
                messages.append(message)

            await app(
                scope,
                receive,
                send,
            )

            headers = dict(
                messages[0]["headers"]
            )

            self.assertEqual(
                messages[0]["status"],
                200,
            )

            self.assertEqual(
                headers[b"content-type"],
                b"text/html; charset=utf-8",
            )

            self.assertEqual(
                messages[1]["body"],
                b"<h1>Aurora Next</h1>",
            )

    async def test_controller_can_return_view(self):
        with tempfile.TemporaryDirectory() as directory:
            template_path = (
                Path(directory) / "profile.html"
            )

            template_path.write_text(
                "<h1>User {{ id }}</h1>",
                encoding="utf-8",
            )

            app = Application(
                views_directory=directory,
            )

            class UserController(Controller):

                def show(self, id):
                    return view(
                        "profile.html",
                        id=id,
                    )

            app.add_route(
                "/users/{id}",
                UserController.action("show"),
            )

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/users/42",
            }

            messages = []

            async def receive():
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }

            async def send(message):
                messages.append(message)

            await app(
                scope,
                receive,
                send,
            )

            self.assertEqual(
                messages[0]["status"],
                200,
            )

            self.assertEqual(
                messages[1]["body"],
                b"<h1>User 42</h1>",
            )

    async def test_view_without_renderer_becomes_500_response(self):
        app = Application()

        def home(request):
            return view(
                "home.html",
            )

        app.add_route("/", home)

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
        }

        messages = []

        async def receive():
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }

        async def send(message):
            messages.append(message)

        await app(
            scope,
            receive,
            send,
        )

        self.assertEqual(
            messages[0]["status"],
            500,
        )

        self.assertEqual(
            messages[1]["body"],
            b"Internal Server Error",
        )


if __name__ == "__main__":
    unittest.main()
