"""MySQL foundation tests that never modify a real database."""

import unittest
from unittest.mock import patch

from sqlalchemy.engine import URL

from aurora.database.database_config import configure_databases
from aurora.database.database_registry import DatabaseRegistry


class MySQLMultiDatabaseFoundationTests(unittest.TestCase):
    def test_mixed_aliases_are_configuration_only(self):
        with patch("aurora.database.database_registry.Database") as factory:
            config = configure_databases({
                "default": {"url": "sqlite:///:memory:"},
                "analytics": {"url": "mysql+pymysql://user:secret@localhost:3306/analytics"},
                "billing": {"url": "mysql+pymysql://user:secret@localhost:3306/billing"},
            })
            registry = DatabaseRegistry(config)
            self.assertEqual(registry.initialized_aliases(), ())
            self.assertEqual(config.resolve("analytics").get_backend_name(), "mysql")
            factory.assert_not_called()

    def test_independent_lazy_engines_and_disposal(self):
        with patch("aurora.database.database_registry.Database") as factory:
            config = configure_databases({
                "default": {"url": "sqlite:///:memory:"},
                "analytics": {"url": "mysql+pymysql://user:secret@localhost/analytics"},
            })
            registry = DatabaseRegistry(config)
            self.assertIs(registry.get("analytics"), registry.get("analytics"))
            self.assertEqual(registry.initialized_aliases(), ("analytics",))
            registry.get("default")
            self.assertEqual(factory.call_count, 2)
            registry.close()
            self.assertEqual(factory.return_value.dispose.call_count, 2)

    def test_unknown_alias_does_not_create_engine(self):
        with patch("aurora.database.database_registry.Database") as factory:
            registry = DatabaseRegistry(configure_databases({
                "default": {"url": "sqlite:///:memory:"},
            }))
            with self.assertRaisesRegex(ValueError, "Unknown database alias"):
                registry.get("missing")
            factory.assert_not_called()

    def test_unsupported_driver_rejected_without_leaking_password(self):
        for url in (
            "mysql://user:private_password@localhost/app",
            "postgresql://user:private_password@localhost/app",
            "mysql+mysqldb://user:private_password@localhost/app",
        ):
            with self.subTest(url=url.split(":", 1)[0]):
                with self.assertRaises(ValueError) as caught:
                    configure_databases({"default": {"url": url}})
                self.assertNotIn("private_password", str(caught.exception))

    def test_mysql_database_name_is_required(self):
        with self.assertRaisesRegex(ValueError, "database name"):
            configure_databases({
                "default": {"url": "mysql+pymysql://user:secret@localhost"},
            })

    def test_url_object_keeps_password_out_of_repr(self):
        url = URL.create("mysql+pymysql", username="app", password="secret", host="localhost", database="app")
        config = configure_databases({"default": {"url": url}})
        self.assertIs(config.resolve(), url)
        self.assertNotIn("secret", repr(config))

    def test_missing_driver_error_does_not_leak_credentials(self):
        config = configure_databases({
            "default": {"url": "mysql+pymysql://user:secret@localhost/app"},
        })
        with patch("aurora.database.database_registry.Database", side_effect=ImportError("secret")):
            with self.assertRaises(RuntimeError) as caught:
                DatabaseRegistry(config).get()
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("default", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
