"""ASGI database work lifecycle contracts using AuroraSQL connections."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from aurora.database.async_routed import RoutedWorkManager
from aurora.database.configuration import configure_databases


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = configure_databases({
            "default": {"url": "sqlite:///" + str(Path(self.temp.name) / "lifecycle.sqlite3")}
        })

    def manager(self, **kwargs):
        return RoutedWorkManager(self.config, **kwargs)

    async def test_invalid_limits(self):
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.manager(max_concurrent=value)

    async def test_empty_close_and_rejection(self):
        work = self.manager()
        await work.aclose()
        await work.aclose()
        with self.assertRaisesRegex(RuntimeError, "closed"):
            await work.run("default", lambda db: None)

    async def test_invalid_callback(self):
        async with self.manager() as work:
            with self.assertRaises(TypeError):
                await work.run("default", None)

            async def callback(db):
                return None

            with self.assertRaises(TypeError):
                await work.run("default", callback)

    async def test_async_context(self):
        async with self.manager() as work:
            self.assertEqual(work.pending_count, 0)
        self.assertTrue(work._closed)

    async def test_shutdown_drains_cancelled_waiter(self):
        started = threading.Event()
        release = threading.Event()

        def worker(db):
            started.set()
            if not release.wait(5):
                raise TimeoutError("worker was not released")
            return 42

        work = self.manager()
        waiter = asyncio.create_task(work.run("default", worker))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(work.pending_count, 1)
            shutdown = asyncio.create_task(work.aclose())
            await asyncio.sleep(0.02)
            self.assertFalse(shutdown.done())
        finally:
            release.set()
            await work.aclose()
        self.assertEqual(work.pending_count, 0)

    async def test_bounded_parallelism(self):
        import time

        lock = threading.Lock()
        active = 0
        peak = 0

        def worker(db):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return 1

        async with self.manager(max_concurrent=2) as work:
            result = await asyncio.gather(*(work.run("default", worker) for _ in range(7)))
        self.assertEqual(result, [1] * 7)
        self.assertLessEqual(peak, 2)
        self.assertGreaterEqual(peak, 1)

    async def test_worker_failure(self):
        def fail(db):
            raise ValueError("fail")

        async with self.manager() as work:
            with self.assertRaisesRegex(ValueError, "fail"):
                await work.run("default", fail)

    async def test_shutdown_rejects_new_work(self):
        work = self.manager()
        await work.aclose()
        with self.assertRaises(RuntimeError):
            async with work:
                pass
