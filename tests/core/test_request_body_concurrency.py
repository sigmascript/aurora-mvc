"""D2-C2C: concurrent body readers must not race on ASGI receive."""

import asyncio
import unittest

from aurora.core.http_exception import HTTPException
from aurora.core.request import Request


class ConcurrentBodyTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_readers_share_one_stream(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0
        messages = iter([
            {"type": "http.request", "body": b"first", "more_body": True},
            {"type": "http.request", "body": b"second", "more_body": False},
        ])

        async def receive():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return next(messages)

        request = Request({"type": "http"}, receive)
        first = asyncio.create_task(request.body())
        await entered.wait()
        second = asyncio.create_task(request.body())
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)
        release.set()

        self.assertEqual(await asyncio.gather(first, second), [b"firstsecond"] * 2)
        self.assertEqual(calls, 2)

    async def test_concurrent_json_and_text_share_body(self):
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b'"hello"', "more_body": False}

        request = Request({"type": "http"}, receive)
        self.assertEqual(await asyncio.gather(request.json(), request.text()), ["hello", '"hello"'])
        self.assertEqual(calls, 1)

    async def test_cancelled_waiter_does_not_cancel_reader(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def receive():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"type": "http.request", "body": b"safe", "more_body": False}

        request = Request({"type": "http"}, receive)
        reader = asyncio.create_task(request.body())
        await entered.wait()
        waiter = asyncio.create_task(request.body())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        self.assertEqual(await reader, b"safe")
        self.assertEqual(await request.body(), b"safe")
        self.assertEqual(calls, 1)

    async def test_body_limit_error_does_not_cache_partial_body(self):
        async def receive():
            return {"type": "http.request", "body": b"oversized", "more_body": False}

        request = Request({"type": "http"}, receive, max_body_bytes=2)
        with self.assertRaises(HTTPException) as error:
            await request.body()
        self.assertEqual(error.exception.status_code, 413)
        self.assertIsNone(request._body)
