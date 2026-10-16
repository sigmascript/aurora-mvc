import unittest
from sqlalchemy.engine import URL
from aurora.database.database_config import DatabaseConfiguration, configure_databases
from aurora.database.model import Model


class DatabaseConfigTests(unittest.TestCase):
    def test_single(self):
        cfg = configure_databases({"default": {"url": "sqlite:///app.db"}})
        self.assertEqual(cfg.aliases(), ("default",))
        self.assertEqual(cfg.resolve().database, "app.db")

    def test_multiple(self):
        cfg = configure_databases({"default": {"url": "sqlite:///app.db"}, "analytics": {"url": "sqlite:///events.db"}})
        self.assertEqual(cfg.resolve("analytics").database, "events.db")

    def test_missing_default(self):
        with self.assertRaises(ValueError): configure_databases({"analytics": {"url": "sqlite:///a.db"}})

    def test_invalid_alias(self):
        with self.assertRaises(ValueError): configure_databases({"default": {"url": "sqlite:///a.db"}, "bad-name": {"url": "sqlite:///b.db"}})

    def test_invalid_url(self):
        with self.assertRaises(ValueError): configure_databases({"default": {"url": "not a url"}})

    def test_missing_url(self):
        with self.assertRaises(ValueError): configure_databases({"default": {}})

    def test_unknown_alias(self):
        with self.assertRaises(ValueError): configure_databases({"default": {"url": "sqlite:///a.db"}}).resolve("other")

    def test_url_object(self):
        url = URL.create("sqlite", database="a.db")
        self.assertIs(configure_databases({"default": {"url": url}}).resolve(), url)

    def test_does_not_bind_model(self):
        before = Model.database
        configure_databases({"default": {"url": "sqlite:///a.db"}})
        self.assertIs(Model.database, before)

    def test_password_not_in_repr(self):
        cfg = DatabaseConfiguration({"default": {"url": "mysql+pymysql://user:secret@localhost/test"}})
        self.assertNotIn("secret", repr(cfg))
