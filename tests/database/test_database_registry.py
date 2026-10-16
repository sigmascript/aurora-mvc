import unittest
from unittest.mock import patch

from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry, create_database_registry


class DatabaseRegistryTests(unittest.TestCase):
    def setUp(self):
        self.config = configure_databases({
            "default": {"url": "sqlite:///:memory:"},
            "analytics": {"url": "sqlite:///:memory:"},
        })

    def test_no_engine_on_creation(self):
        with patch("aurora.database.database_registry.Database") as factory:
            registry = create_database_registry(self.config)
            self.assertEqual(registry.initialized_aliases(), ())
            factory.assert_not_called()

    def test_aliases(self):
        self.assertEqual(DatabaseRegistry(self.config).aliases(), ("default", "analytics"))

    def test_invalid_configuration(self):
        with self.assertRaises(TypeError):
            DatabaseRegistry({"default": {"url": "sqlite://"}})

    def test_unknown_alias_never_creates_engine(self):
        with patch("aurora.database.database_registry.Database") as factory:
            registry = DatabaseRegistry(self.config)
            with self.assertRaises(ValueError):
                registry.get("missing")
            factory.assert_not_called()

    def test_lazy_and_cached(self):
        with DatabaseRegistry(self.config) as registry:
            first = registry.get()
            self.assertIs(first, registry.get("default"))
            self.assertEqual(registry.initialized_aliases(), ("default",))

    def test_independent_instances(self):
        with DatabaseRegistry(self.config) as registry:
            self.assertIsNot(registry.get(), registry.get("analytics"))
            self.assertEqual(set(registry.initialized_aliases()), {"default", "analytics"})

    def test_dispose_only_created_instances(self):
        with patch("aurora.database.database_registry.Database") as factory:
            registry = DatabaseRegistry(self.config)
            registry.get("analytics")
            registry.close()
            factory.assert_called_once()
            factory.return_value.dispose.assert_called_once()

    def test_close_idempotent(self):
        with patch("aurora.database.database_registry.Database") as factory:
            registry = DatabaseRegistry(self.config)
            registry.get()
            registry.close()
            registry.close()
            factory.return_value.dispose.assert_called_once()

    def test_get_after_close_rejected(self):
        registry = DatabaseRegistry(self.config)
        registry.close()
        with self.assertRaises(RuntimeError):
            registry.get()

    def test_enter_after_close_rejected(self):
        registry = DatabaseRegistry(self.config)
        registry.close()
        with self.assertRaises(RuntimeError):
            with registry:
                pass

    def test_context_manager_closes(self):
        with DatabaseRegistry(self.config) as registry:
            registry.get()
        with self.assertRaises(RuntimeError):
            registry.get()

    def test_does_not_mutate_model_binding(self):
        from aurora.database.model import Model
        before = Model.database
        with DatabaseRegistry(self.config) as registry:
            registry.get("analytics")
            self.assertIs(Model.database, before)
