"""Verify worker ownership, event-loop progress, and scope restoration."""

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from aurora.aurorasql import Model, column, run_database_work


class WorkerBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_is_offloaded_and_scope_is_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            url = f"sqlite:///{(Path(folder) / 'work.sqlite3').as_posix()}"
            main_thread = threading.get_ident()
            class Item(Model):
                id = column(int, primary_key=True)

            def work(connection):
                assert Item._operations().connection is connection
                connection.execute_write("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
                with connection.transaction():
                    connection.execute_write("INSERT INTO items (value) VALUES (:value)", {"value": "ok"})
                return threading.get_ident(), connection.fetch_first("SELECT COUNT(*) AS total FROM items")["total"]
            worker_thread, count = await run_database_work(url, work)
            self.assertNotEqual(worker_thread, main_thread)
            self.assertEqual(count, 1)
            self.assertIsNone(Model.database)

    async def test_event_loop_progresses_during_blocked_work(self):
        with tempfile.TemporaryDirectory() as folder:
            url = f"sqlite:///{(Path(folder) / 'work.sqlite3').as_posix()}"
            started = threading.Event()
            release = threading.Event()
            def work(connection):
                started.set()
                if not release.wait(3):
                    raise RuntimeError("Timed out waiting for test")
                return connection._closed
            task = asyncio.create_task(run_database_work(url, work))
            try:
                ready = await asyncio.to_thread(started.wait, 2)
                self.assertTrue(ready)
                await asyncio.wait_for(asyncio.sleep(0), timeout=1)
            finally:
                release.set()
            self.assertFalse(await task)

    async def test_exception_closes_connection_and_restores_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            url = f"sqlite:///{(Path(folder) / 'work.sqlite3').as_posix()}"
            observed = []
            def work(connection):
                observed.append(connection)
                raise ValueError("intentional failure")
            with self.assertRaisesRegex(ValueError, "intentional failure"):
                await run_database_work(url, work)
            self.assertTrue(observed[0]._closed)
            self.assertIsNone(Model.database)

    async def test_rejects_async_callback(self):
        async def callback(connection):
            return None
        with self.assertRaises(TypeError):
            await run_database_work("sqlite:///:memory:", callback)
