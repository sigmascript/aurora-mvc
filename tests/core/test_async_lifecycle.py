import asyncio
import threading
import unittest
from aurora.database.async_routed import RoutedWorkManager


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_limits(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                RoutedWorkManager(None, max_concurrent=value)

    async def test_empty_close_and_rejection(self):
        work = RoutedWorkManager(None)
        await work.aclose()
        await work.aclose()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            await work.run(None, lambda tx: None)

    async def test_invalid_callback(self):
        work = RoutedWorkManager(None)
        with self.assertRaises(TypeError):
            await work.run(None, None)
        async def cb(tx):
            pass
        with self.assertRaises(TypeError):
            await work.run(None, cb)
        await work.aclose()

    async def test_async_context(self):
        async with RoutedWorkManager(None) as work:
            self.assertEqual(work.pending_count, 0)
        self.assertTrue(work._closed)

    async def test_shutdown_drains_cancelled_waiter(self):
        from unittest.mock import patch
        started = threading.Event()
        release = threading.Event()
        def worker(access, model, callback):
            started.set()
            release.wait(5)
            return 42
        with patch('aurora.database.async_routed._run_sync', worker):
            work = RoutedWorkManager(None)
            waiter = asyncio.create_task(work.run(None, lambda tx: None))
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await waiter
            self.assertEqual(work.pending_count, 1)
            shutdown = asyncio.create_task(work.aclose())
            await asyncio.sleep(.02)
            self.assertFalse(shutdown.done())
            release.set()
            await shutdown
            self.assertEqual(work.pending_count, 0)

    async def test_bounded_parallelism(self):
        from unittest.mock import patch
        lock = threading.Lock()
        active = [0]
        peak = [0]
        def worker(access, model, callback):
            import time
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(.03)
            with lock:
                active[0] -= 1
            return 1
        with patch('aurora.database.async_routed._run_sync', worker):
            async with RoutedWorkManager(None, max_concurrent=2) as work:
                result = await asyncio.gather(*(work.run(None, lambda tx: None) for _ in range(7)))
            self.assertEqual(result, [1] * 7)
            self.assertLessEqual(peak[0], 2)

    async def test_worker_failure(self):
        from unittest.mock import patch
        with patch('aurora.database.async_routed._run_sync', side_effect=ValueError('fail')):
            async with RoutedWorkManager(None) as work:
                with self.assertRaisesRegex(ValueError, 'fail'):
                    await work.run(None, lambda tx: None)

    async def test_shutdown_rejects_new_work(self):
        work = RoutedWorkManager(None)
        await work.aclose()
        with self.assertRaises(RuntimeError):
            async with work:
                pass
