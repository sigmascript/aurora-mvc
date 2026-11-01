"""Modern AuroraSQL routed async transactions and alias isolation."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora.database.configuration import configure_databases
from aurora.database.async_routed import RoutedWorkManager


class AsyncRoutedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.config = configure_databases({
            "default": {"url": "sqlite:///" + str(root / "core.db")},
            "analytics": {"url": "sqlite:///" + str(root / "analytics.db")},
        })
        self.manager = RoutedWorkManager(self.config)
        await self.manager.run("default", lambda db: db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY)"))
        await self.manager.run("analytics", lambda db: db.execute("CREATE TABLE events(id INTEGER PRIMARY KEY)"))

    async def asyncTearDown(self):
        await self.manager.aclose()

    async def test_create_and_read(self):
        await self.manager.run_transaction("analytics", lambda db: db.execute("INSERT INTO events VALUES (1)"))
        result = await self.manager.run("analytics", lambda db: db.execute("SELECT id FROM events"))
        self.assertEqual(len(result), 1)

    async def test_rollback(self):
        def fail(db):
            db.execute("INSERT INTO events VALUES (2)")
            raise ValueError("abort")
        with self.assertRaisesRegex(ValueError, "abort"):
            await self.manager.run_transaction("analytics", fail)
        rows = await self.manager.run("analytics", lambda db: db.execute("SELECT id FROM events"))
        self.assertEqual(rows, [])

    async def test_parallel_alias_isolation(self):
        await asyncio.gather(
            self.manager.run_transaction("default", lambda db: db.execute("INSERT INTO events VALUES (3)")),
            self.manager.run_transaction("analytics", lambda db: db.execute("INSERT INTO events VALUES (4)")),
        )
        core = await self.manager.run("default", lambda db: db.execute("SELECT id FROM events"))
        analytics = await self.manager.run("analytics", lambda db: db.execute("SELECT id FROM events"))
        self.assertEqual(core[0]["id"], 3)
        self.assertEqual(analytics[0]["id"], 4)

    async def test_async_callback_rejected(self):
        async def callback(db):
            return None
        with self.assertRaisesRegex(TypeError, "synchronous"):
            await self.manager.run_transaction("default", callback)

    async def test_invalid_callback_rejected(self):
        with self.assertRaises(TypeError):
            await self.manager.run_transaction("default", None)

    async def test_connection_escape_rejected(self):
        with self.assertRaisesRegex(TypeError, "connection-bound"):
            await self.manager.run_transaction("default", lambda db: db)

    async def test_unknown_alias_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown database alias"):
            await self.manager.run_transaction("missing", lambda db: None)
