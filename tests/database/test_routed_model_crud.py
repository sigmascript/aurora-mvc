import unittest
from aurora.database.routed_transactions import routed_transaction
import tempfile
from pathlib import Path
from aurora.database import Model, column
from aurora.database.registry import ModelRegistry
from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import configure_model_routing
from aurora.database.routed_access import RoutedDatabaseAccess


class RoutedModelCrudTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class CoreItem(Model):
            __table__ = "phase6108_core_item"
            id = column(int, primary_key=True)
        class EventItem(Model):
            __table__ = "phase6108_event_item"
            id = column(int, primary_key=True)
        cls.core, cls.event = CoreItem, EventItem

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        registry = ModelRegistry()
        registry.register(self.core)
        registry.register(self.event, app="analytics")
        cfg = configure_databases({
            "default": {"url": "sqlite:///" + str(Path(tmp.name) / "core.db")},
            "analytics": {"url": "sqlite:///" + str(Path(tmp.name) / "analytics.db")},
        })
        databases = DatabaseRegistry(cfg)
        self.addCleanup(databases.close)
        routing = configure_model_routing(registry, cfg, {self.event: "analytics"})
        self.access = RoutedDatabaseAccess(routing, databases)
        for model in (self.core, self.event):
            model.__sql_table__.create(self.access.database_for(model).engine)

    def test_create_read_update_delete(self):
        with routed_transaction(self.access, self.event) as tx:
            ops = tx.model(self.event)
            item = ops.create(id=41)
            self.assertEqual(item.id, 41)
            self.assertEqual(ops.first(where={"id": 41}).id, 41)
            self.assertEqual(len(ops.all()), 1)
            self.assertEqual(ops.update(values={"id": 42}, where={"id": 41}), 1)
            self.assertEqual(ops.delete(where={"id": 42}), 1)
            self.assertEqual(ops.all(), [])

    def test_cross_database_model_rejected(self):
        with routed_transaction(self.access, self.core) as tx:
            with self.assertRaisesRegex(ValueError, "different database"):
                tx.model(self.event)

    def test_unregistered_model_rejected(self):
        with routed_transaction(self.access, self.core) as tx:
            with self.assertRaises((KeyError, ValueError, TypeError)):
                tx.model(object)

    def test_closed_model_operations_rejected(self):
        with routed_transaction(self.access, self.core) as tx:
            ops = tx.model(self.core)
        with self.assertRaisesRegex(RuntimeError, "closed"):
            ops.all()

    def test_whole_table_safeguards(self):
        with routed_transaction(self.access, self.core) as tx:
            ops = tx.model(self.core)
            with self.assertRaisesRegex(RuntimeError, "confirm"):
                ops.delete()
            with self.assertRaisesRegex(RuntimeError, "confirm"):
                ops.update(values={"id": 2})

    def test_rollback_model_create(self):
        with self.assertRaisesRegex(ValueError, "abort"):
            with routed_transaction(self.access, self.event) as tx:
                tx.model(self.event).create(id=7)
                raise ValueError("abort")
        with routed_transaction(self.access, self.event) as tx:
            self.assertEqual(tx.model(self.event).all(), [])

    def test_model_binding_unchanged(self):
        from aurora.database import Model
        old = Model.database
        with routed_transaction(self.access, self.event) as tx:
            tx.model(self.event).create(id=9)
        self.assertIs(Model.database, old)
