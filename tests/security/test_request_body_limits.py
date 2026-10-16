"""Checkpoint B2: bounded request-body reads and cancellation behavior."""
import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.request import Request


class RequestBodyLimitTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def make_receive(messages):
        iterator = iter(messages)

        async def receive():
            return next(iterator)

        return receive

    async def test_exact_limit_and_cached_body(self):
        request = Request(
            {"type": "http"},
            self.make_receive([
                {"type": "http.request", "body": b"ab", "more_body": True},
                {"type": "http.request", "body": b"cde", "more_body": False},
            ]),
            max_body_bytes=5,
        )
        self.assertEqual(await request.body(), b"abcde")
        self.assertEqual(await request.body(), b"abcde")

    async def test_limit_enforced_across_chunks_without_caching_partial_body(self):
        request = Request(
            {"type": "http"},
            self.make_receive([
                {"type": "http.request", "body": b"abc", "more_body": True},
                {"type": "http.request", "body": b"def", "more_body": False},
            ]),
            max_body_bytes=5,
        )
        with self.assertRaises(HTTPException) as caught:
            await request.body()
        self.assertEqual(caught.exception.status_code, 413)
        self.assertIsNone(request._body)

    async def test_zero_limit_allows_empty_only(self):
        request = Request({"type": "http"}, self.make_receive([
            {"type": "http.request", "body": b"", "more_body": False},
        ]), max_body_bytes=0)
        self.assertEqual(await request.body(), b"")
        request = Request({"type": "http"}, self.make_receive([
            {"type": "http.request", "body": b"x", "more_body": False},
        ]), max_body_bytes=0)
        with self.assertRaises(HTTPException) as caught:
            await request.body()
        self.assertEqual(caught.exception.status_code, 413)

    async def test_cancellation_propagates_and_does_not_cache(self):
        async def receive():
            raise asyncio.CancelledError()

        request = Request({"type": "http"}, receive, max_body_bytes=10)
        with self.assertRaises(asyncio.CancelledError):
            await request.body()
        self.assertIsNone(request._body)

    async def test_application_returns_413_when_limit_exceeded(self):
        app = Application(max_request_body_bytes=3)

        async def handler(request):
            await request.body()
            return "not reached"

        app.add_route("/upload", handler, methods=["POST"])
        sent = []
        receive = self.make_receive([
            {"type": "http.request", "body": b"four", "more_body": False},
        ])

        async def send(message):
            sent.append(message)

        await app({"type": "http", "path": "/upload", "method": "POST", "headers": []}, receive, send)
        self.assertEqual(sent[0]["status"], 413)

    async def test_application_default_remains_unlimited(self):
        app = Application()
        self.assertIsNone(app.max_request_body_bytes)

    async def test_invalid_limits_are_rejected(self):
        for value in (-1, True, 1.2, "1024"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    Request({"type": "http"}, max_body_bytes=value)
                with self.assertRaises(ValueError):
                    Application(max_request_body_bytes=value)
