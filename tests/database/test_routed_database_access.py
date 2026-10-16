import unittest

from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import configure_model_routing
from aurora.database.registry import ModelRegistry
from aurora.database.routed_access import RoutedDatabaseAccess
from aurora.database.model import Model


class RoutedAccessTests(unittest.TestCase):
    def setUp(self):
        # Existing model classes avoid changing global SQLAlchemy metadata.
        from tests.fixtures.database_probe import DatabaseProbe
        self.model = DatabaseProbe
        self.models = ModelRegistry()
        self.models.register(DatabaseProbe)
        self.config = configure_databases({
            'default': {'url': 'sqlite:///:memory:'},
            'analytics': {'url': 'sqlite:///:memory:'},
        })
        self.databases = DatabaseRegistry(self.config)
        self.addCleanup(self.databases.close)

    def test_default_is_lazy(self):
        routing = configure_model_routing(self.models, self.config)
        access = RoutedDatabaseAccess(routing, self.databases)
        self.assertEqual(access.alias_for(self.model), 'default')
        self.assertEqual(self.databases.initialized_aliases(), ())
        self.assertIs(access.database_for(self.model), self.databases.get('default'))
        self.assertEqual(self.databases.initialized_aliases(), ('default',))

    def test_named_alias_is_lazy(self):
        routing = configure_model_routing(self.models, self.config, {self.model: 'analytics'})
        access = RoutedDatabaseAccess(routing, self.databases)
        self.assertEqual(access.alias_for(self.model), 'analytics')
        self.assertEqual(self.databases.initialized_aliases(), ())
        self.assertIs(access.database_for(self.model), self.databases.get('analytics'))
        self.assertEqual(self.databases.initialized_aliases(), ('analytics',))

    def test_unregistered_model_rejected(self):
        access = RoutedDatabaseAccess(configure_model_routing(self.models, self.config), self.databases)
        with self.assertRaises(KeyError):
            access.database_for('unknown.Model')
        self.assertEqual(self.databases.initialized_aliases(), ())

    def test_configuration_mismatch_rejected_without_engines(self):
        routing = configure_model_routing(self.models, self.config, {self.model: 'analytics'})
        restricted = DatabaseRegistry(configure_databases({'default': {'url': 'sqlite:///:memory:'}}))
        self.addCleanup(restricted.close)
        with self.assertRaises(ValueError):
            RoutedDatabaseAccess(routing, restricted)
        self.assertEqual(restricted.initialized_aliases(), ())

    def test_no_global_model_rebinding(self):
        old = Model.database
        access = RoutedDatabaseAccess(configure_model_routing(self.models, self.config), self.databases)
        access.database_for(self.model)
        self.assertIs(Model.database, old)

    def test_rejects_invalid_arguments(self):
        routing = configure_model_routing(self.models, self.config)
        with self.assertRaises(TypeError):
            RoutedDatabaseAccess(None, self.databases)
        with self.assertRaises(TypeError):
            RoutedDatabaseAccess(routing, None)

    def test_closed_registry_rejected(self):
        access = RoutedDatabaseAccess(configure_model_routing(self.models, self.config), self.databases)
        self.databases.close()
        with self.assertRaises(RuntimeError):
            access.database_for(self.model)
