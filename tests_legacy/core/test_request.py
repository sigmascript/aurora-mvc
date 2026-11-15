import unittest

from aurora.core.http_exception import HTTPException
from aurora.core.request import Request


class TestRequest(unittest.IsolatedAsyncioTestCase):

    async def test_request_properties(self):
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/users",
            "query_string": b"page=2&active=true",
            "headers": [
                (b"host", b"localhost"),
                (b"content-type", b"application/json"),
            ],
        }

        request = Request(scope)

        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/users")
        self.assertEqual(
            request.query_string,
            "page=2&active=true",
        )
        self.assertEqual(
            request.headers["content-type"],
            "application/json",
        )

    async def test_request_body(self):
        messages = [
            {
                "type": "http.request",
                "body": b"Hello Aurora",
                "more_body": False,
            },
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/message",
            },
            receive,
        )

        body = await request.body()

        self.assertEqual(
            body,
            b"Hello Aurora",
        )

    async def test_request_body_multiple_chunks(self):
        messages = [
            {
                "type": "http.request",
                "body": b"Hello ",
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": b"from ",
                "more_body": True,
            },
            {
                "type": "http.request",
                "body": b"Aurora",
                "more_body": False,
            },
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/message",
            },
            receive,
        )

        body = await request.body()

        self.assertEqual(
            body,
            b"Hello from Aurora",
        )

    async def test_request_body_is_cached(self):
        receive_calls = 0

        async def receive():
            nonlocal receive_calls

            receive_calls += 1

            return {
                "type": "http.request",
                "body": b"Cached body",
                "more_body": False,
            }

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/message",
            },
            receive,
        )

        first_body = await request.body()
        second_body = await request.body()

        self.assertEqual(
            first_body,
            b"Cached body",
        )

        self.assertEqual(
            second_body,
            b"Cached body",
        )

        self.assertEqual(
            receive_calls,
            1,
        )

    async def test_request_body_without_receive_is_empty(self):
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/",
        })

        body = await request.body()

        self.assertEqual(
            body,
            b"",
        )

    async def test_request_text(self):
        messages = [
            {
                "type": "http.request",
                "body": "Hello Aurora".encode("utf-8"),
                "more_body": False,
            },
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/message",
            },
            receive,
        )

        text = await request.text()

        self.assertEqual(
            text,
            "Hello Aurora",
        )

    async def test_request_json(self):
        messages = [
            {
                "type": "http.request",
                "body": b'{"name":"Aurora","version":1}',
                "more_body": False,
            },
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api",
            },
            receive,
        )

        data = await request.json()

        self.assertEqual(
            data,
            {
                "name": "Aurora",
                "version": 1,
            },
        )

    async def test_invalid_json_raises_http_exception(self):
        messages = [
            {
                "type": "http.request",
                "body": b'{"name":',
                "more_body": False,
            },
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api",
            },
            receive,
        )

        with self.assertRaises(HTTPException) as context:
            await request.json()

        self.assertEqual(
            context.exception.status_code,
            400,
        )

        self.assertEqual(
            context.exception.detail,
            "Invalid JSON",
        )

    async def test_invalid_json_encoding_raises_http_exception(self):
        messages = [
            {
                "type": "http.request",
                "body": b"\xff\xfe",
                "more_body": False,
            },
        ]

        async def receive():
            return messages.pop(0)

        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api",
            },
            receive,
        )

        with self.assertRaises(HTTPException) as context:
            await request.json()

        self.assertEqual(
            context.exception.status_code,
            400,
        )

        self.assertEqual(
            context.exception.detail,
            "Invalid JSON",
        )


if __name__ == "__main__":
    unittest.main()
