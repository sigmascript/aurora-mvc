import unittest
from unittest.mock import patch
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


class EnvironmentDatabaseConfigTests(unittest.TestCase):
    def test_environment_sqlite_and_mysql(self):
        with patch.dict('os.environ', {'AURORA_TEST_SQLITE': 'sqlite:///private.db',
                                       'AURORA_TEST_MYSQL': 'mysql+pymysql://person:secret@localhost/app'}):
            cfg = configure_databases({'default': {'url_env': 'AURORA_TEST_SQLITE'},
                                       'analytics': {'url_env': 'AURORA_TEST_MYSQL'}})
        self.assertEqual(cfg.resolve().database, 'private.db')
        self.assertEqual(cfg.resolve('analytics').database, 'app')
        self.assertNotIn('secret', repr(cfg))

    def test_environment_snapshot_not_reloaded(self):
        with patch.dict('os.environ', {'AURORA_TEST_DB': 'sqlite:///first.db'}):
            cfg = configure_databases({'default': {'url_env': 'AURORA_TEST_DB'}})
        self.assertEqual(cfg.resolve().database, 'first.db')

    def test_mutual_exclusion(self):
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            configure_databases({'default': {'url': 'sqlite:///x.db', 'url_env': 'AURORA_TEST_DB'}})

    def test_missing_and_empty_environment(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'nonempty environment'):
                configure_databases({'default': {'url_env': 'AURORA_TEST_DB'}})
        with patch.dict('os.environ', {'AURORA_TEST_DB': '   '}):
            with self.assertRaisesRegex(ValueError, 'nonempty environment'):
                configure_databases({'default': {'url_env': 'AURORA_TEST_DB'}})

    def test_invalid_environment_names(self):
        for name in ('', '1BAD', 'BAD-NAME', ' BAD', 123, None):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'invalid environment'):
                configure_databases({'default': {'url_env': name}})

    def test_invalid_url_does_not_leak_password(self):
        secret = 'supersecret123'
        with patch.dict('os.environ', {'AURORA_TEST_DB': 'not-a-url:' + secret}):
            with self.assertRaises(ValueError) as error:
                configure_databases({'default': {'url_env': 'AURORA_TEST_DB'}})
        self.assertNotIn(secret, str(error.exception))

    def test_unsupported_driver_and_unknown_settings(self):
        with patch.dict('os.environ', {'AURORA_TEST_DB': 'postgresql://u:secret@localhost/db'}):
            with self.assertRaisesRegex(ValueError, 'unsupported driver'):
                configure_databases({'default': {'url_env': 'AURORA_TEST_DB'}})
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            configure_databases({'default': {'url_env': 'AURORA_TEST_DB', 'extra': True}})
