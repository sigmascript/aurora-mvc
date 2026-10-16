import tempfile
import unittest
from pathlib import Path
from sqlalchemy import text
from aurora.database import Model, column
from aurora.database.registry import ModelRegistry
from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import configure_model_routing
from aurora.database.routed_access import RoutedDatabaseAccess
from aurora.database.routed_transactions import routed_transaction


class RoutedTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class CoreItem(Model):
            __table__ = 'phase6107_core_item'
            id = column(int, primary_key=True)
        class EventItem(Model):
            __table__ = 'phase6107_event_item'
            id = column(int, primary_key=True)
        cls.core, cls.event = CoreItem, EventItem

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CoreItem, EventItem = self.core, self.event
        registry = ModelRegistry()
        registry.register(CoreItem)
        registry.register(EventItem, app='analytics')
        cfg = configure_databases({
            'default': {'url': 'sqlite:///' + str(Path(self.tmp.name) / 'core.db')},
            'analytics': {'url': 'sqlite:///' + str(Path(self.tmp.name) / 'analytics.db')},
        })
        self.databases = DatabaseRegistry(cfg)
        self.addCleanup(self.databases.close)
        routing = configure_model_routing(registry, cfg, {EventItem: 'analytics'})
        self.access = RoutedDatabaseAccess(routing, self.databases)
        for model in (CoreItem, EventItem):
            db = self.access.database_for(model)
            model.__sql_table__.create(db.engine)

    def test_commit(self):
        with routed_transaction(self.access, self.event) as tx:
            tx.execute_write(self.event.__sql_table__.insert().values(id=1))
        with routed_transaction(self.access, self.event) as tx:
            self.assertEqual(len(tx.fetch_all(self.event.__sql_table__.select())), 1)

    def test_rollback(self):
        with self.assertRaisesRegex(ValueError, 'abort'):
            with routed_transaction(self.access, self.event) as tx:
                tx.execute_write(self.event.__sql_table__.insert().values(id=2))
                raise ValueError('abort')
        with routed_transaction(self.access, self.event) as tx:
            self.assertEqual(tx.fetch_all(self.event.__sql_table__.select()), [])

    def test_independent_databases(self):
        with routed_transaction(self.access, self.core) as tx:
            tx.execute_write(self.core.__sql_table__.insert().values(id=3))
        with routed_transaction(self.access, self.event) as tx:
            self.assertEqual(tx.fetch_all(self.event.__sql_table__.select()), [])

    def test_closed_context_rejected(self):
        with routed_transaction(self.access, self.core) as tx:
            self.assertEqual(tx.fetch_first('select 1 as n')['n'], 1)
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            tx.execute('select 1')

    def test_invalid_model_rejected(self):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            with routed_transaction(self.access, object):
                pass

    def test_no_global_model_rebinding(self):
        before = Model.database
        with routed_transaction(self.access, self.event) as tx:
            tx.execute('select 1')
        self.assertIs(Model.database, before)

    def test_independent_nested_scopes(self):
        with routed_transaction(self.access, self.core) as core_tx:
            with routed_transaction(self.access, self.event) as event_tx:
                self.assertEqual(core_tx.fetch_first('select 1 as n')['n'], 1)
                self.assertEqual(event_tx.fetch_first('select 2 as n')['n'], 2)
