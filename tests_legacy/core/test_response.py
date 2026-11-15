import json
import unittest

from aurora.core.response import (
    JSONResponse,
    RedirectResponse,
    Response,
)


class TestResponse(unittest.IsolatedAsyncioTestCase):

    async def test_text_response(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = Response("Hello Aurora")

        await response(send)

        self.assertEqual(
            messages[0]["type"],
            "http.response.start",
        )

        self.assertEqual(
            messages[0]["status"],
            200,
        )

        self.assertEqual(
            messages[1]["type"],
            "http.response.body",
        )

        self.assertEqual(
            messages[1]["body"],
            b"Hello Aurora",
        )

    async def test_custom_headers(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = Response(
            "Hello Aurora",
            headers={
                "X-Framework": "Aurora",
                "Cache-Control": "no-cache",
            },
        )

        await response(send)

        headers = dict(
            messages[0]["headers"]
        )

        self.assertEqual(
            headers[b"x-framework"],
            b"Aurora",
        )

        self.assertEqual(
            headers[b"cache-control"],
            b"no-cache",
        )

    async def test_content_headers_are_managed_by_response(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = Response(
            "Hello",
            headers={
                "Content-Type": "wrong/type",
                "Content-Length": "999",
            },
        )

        await response(send)

        headers = dict(
            messages[0]["headers"]
        )

        self.assertEqual(
            headers[b"content-type"],
            b"text/plain; charset=utf-8",
        )

        self.assertEqual(
            headers[b"content-length"],
            b"5",
        )

    async def test_json_response(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = JSONResponse({
            "name": "Aurora",
            "version": 1,
        })

        await response(send)

        headers = dict(
            messages[0]["headers"]
        )

        body = json.loads(
            messages[1]["body"].decode("utf-8")
        )

        self.assertEqual(
            messages[0]["status"],
            200,
        )

        self.assertEqual(
            headers[b"content-type"],
            b"application/json; charset=utf-8",
        )

        self.assertEqual(
            body,
            {
                "name": "Aurora",
                "version": 1,
            },
        )

    async def test_json_response_supports_unicode(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = JSONResponse({
            "message": "سڵاو",
        })

        await response(send)

        body = messages[1]["body"].decode(
            "utf-8"
        )

        self.assertIn(
            "سڵاو",
            body,
        )

    async def test_redirect_response(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = RedirectResponse("/login")

        await response(send)

        headers = dict(
            messages[0]["headers"]
        )

        self.assertEqual(
            messages[0]["status"],
            302,
        )

        self.assertEqual(
            headers[b"location"],
            b"/login",
        )

        self.assertEqual(
            messages[1]["body"],
            b"",
        )

    async def test_redirect_response_custom_status(self):
        messages = []

        async def send(message):
            messages.append(message)

        response = RedirectResponse(
            "/new-location",
            status_code=301,
        )

        await response(send)

        self.assertEqual(
            messages[0]["status"],
            301,
        )


if __name__ == "__main__":
    unittest.main()
