import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora.database import Model, column
from aurora.database.registry import ModelRegistry
from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import configure_model_routing
from aurora.database.routed_access import RoutedDatabaseAccess
from aurora.database.async_routed import run_routed_transaction


class AsyncRoutedTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        class AsyncCore(Model):
            __table__ = "phase6109_core"
            id = column(int, primary_key=True)
        class AsyncEvent(Model):
            __table__ = "phase6109_event"
            id = column(int, primary_key=True)
        cls.core, cls.event = AsyncCore, AsyncEvent

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        models = ModelRegistry()
        models.register(self.core)
        models.register(self.event, app="analytics")
        cfg = configure_databases({
            "default": {"url": "sqlite:///" + str(Path(self.tmp.name) / "core.db")},
            "analytics": {"url": "sqlite:///" + str(Path(self.tmp.name) / "analytics.db")},
        })
        dbs = DatabaseRegistry(cfg)
        self.addCleanup(dbs.close)
        routing = configure_model_routing(models, cfg, {self.event: "analytics"})
        self.access = RoutedDatabaseAccess(routing, dbs)
        for model in (self.core, self.event):
            model.__sql_table__.create(self.access.database_for(model).engine)

    async def test_create_and_read(self):
        await run_routed_transaction(self.access, self.event, lambda tx: tx.model(self.event).create(id=1).id)
        value = await run_routed_transaction(self.access, self.event, lambda tx: tx.model(self.event).first(where={"id": 1}).id)
        self.assertEqual(value, 1)

    async def test_rollback(self):
        def fail(tx):
            tx.model(self.event).create(id=2)
            raise ValueError("abort")
        with self.assertRaisesRegex(ValueError, "abort"):
            await run_routed_transaction(self.access, self.event, fail)
        rows = await run_routed_transaction(self.access, self.event, lambda tx: tx.model(self.event).all())
        self.assertEqual(rows, [])

    async def test_parallel_separate_databases(self):
        await asyncio.gather(
            run_routed_transaction(self.access, self.core, lambda tx: tx.model(self.core).create(id=3).id),
            run_routed_transaction(self.access, self.event, lambda tx: tx.model(self.event).create(id=4).id),
        )
        self.assertEqual((await run_routed_transaction(self.access, self.core, lambda tx: tx.model(self.core).all()))[0].id, 3)
        self.assertEqual((await run_routed_transaction(self.access, self.event, lambda tx: tx.model(self.event).all()))[0].id, 4)

    async def test_async_callback_rejected(self):
        async def callback(tx):
            return None
        with self.assertRaisesRegex(TypeError, "synchronous"):
            await run_routed_transaction(self.access, self.core, callback)

    async def test_invalid_callback_rejected(self):
        with self.assertRaises(TypeError):
            await run_routed_transaction(self.access, self.core, None)

    async def test_transaction_escape_rejected(self):
        with self.assertRaisesRegex(ValueError, "escape"):
            await run_routed_transaction(self.access, self.core, lambda tx: tx)

    async def test_bound_operations_escape_rejected(self):
        with self.assertRaisesRegex(ValueError, "escape"):
            await run_routed_transaction(self.access, self.core, lambda tx: tx.model(self.core))

    async def test_no_global_rebind(self):
        original = Model.database
        await run_routed_transaction(self.access, self.core, lambda tx: tx.model(self.core).all())
        self.assertIs(Model.database, original)
