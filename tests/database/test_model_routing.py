import unittest
from unittest.mock import patch

from aurora.database.database_config import configure_databases
from aurora.database.model import Model
from aurora.database.registry import ModelRegistry
from aurora.database.model_routing import configure_model_routing


class ModelRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Distinct names avoid shared metadata collisions with other tests.
        from aurora.database.column import column
        class RoutingTestUser(Model):
            id = column(int, primary_key=True)
        class RoutingTestEvent(Model):
            id = column(int, primary_key=True)
        cls.user = RoutingTestUser
        cls.event = RoutingTestEvent

    def setUp(self):
        self.models = ModelRegistry()
        self.models.register(self.user)
        self.models.register(self.event, app='analytics')
        self.config = configure_databases({'default': {'url': 'sqlite:///:memory:'},
                                           'analytics': {'url': 'sqlite:///:memory:'}})

    def test_default_route(self):
        routing = configure_model_routing(self.models, self.config)
        self.assertEqual(routing.resolve(self.user), 'default')
        self.assertEqual(routing.resolve('analytics.RoutingTestEvent'), 'default')

    def test_explicit_class_route(self):
        routing = configure_model_routing(self.models, self.config, {self.event: 'analytics'})
        self.assertEqual(routing.resolve(self.event), 'analytics')
        self.assertEqual(routing.resolve(self.user), 'default')

    def test_qualified_name_route(self):
        routing = configure_model_routing(self.models, self.config,
                                          {'analytics.RoutingTestEvent': 'analytics'})
        self.assertEqual(routing.resolve(self.event), 'analytics')

    def test_unregistered_route_rejected(self):
        with self.assertRaises(ValueError):
            configure_model_routing(self.models, self.config, {'Missing': 'default'})

    def test_unknown_database_rejected(self):
        with self.assertRaises(ValueError):
            configure_model_routing(self.models, self.config, {self.user: 'missing'})

    def test_duplicate_assignment_rejected(self):
        with self.assertRaises(ValueError):
            configure_model_routing(self.models, self.config, {self.user: 'default', 'RoutingTestUser': 'analytics'})

    def test_unknown_lookup_rejected(self):
        with self.assertRaises(KeyError):
            configure_model_routing(self.models, self.config).resolve('missing')

    def test_invalid_arguments(self):
        with self.assertRaises(TypeError):
            configure_model_routing(None, self.config)
        with self.assertRaises(TypeError):
            configure_model_routing(self.models, None)
        with self.assertRaises(TypeError):
            configure_model_routing(self.models, self.config, [])

    def test_does_not_create_database(self):
        with patch('aurora.database.database.Database') as db:
            configure_model_routing(self.models, self.config, {self.event: 'analytics'})
            db.assert_not_called()

    def test_does_not_change_binding(self):
        previous = Model.database
        configure_model_routing(self.models, self.config, {self.event: 'analytics'})
        self.assertIs(Model.database, previous)

    def test_deterministic_assignments(self):
        routing = configure_model_routing(self.models, self.config, {self.event: 'analytics'})
        self.assertEqual(routing.assignments(), (('RoutingTestUser', 'default'), ('analytics.RoutingTestEvent', 'analytics')))
