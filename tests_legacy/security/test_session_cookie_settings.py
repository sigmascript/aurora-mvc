"""B3: validate session cookie names before creating a session store."""
import tempfile
import unittest
from pathlib import Path
from aurora.security.sessions import SessionSettings


class SessionCookieSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "sessions.sqlite3")

    def test_rejects_invalid_cookie_name_separators(self):
        for name in ("session/name", "session:name", "session,name", "session(name)", "session\\name", "session name"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                SessionSettings(database_path=self.db, cookie_name=name)

    def test_rejects_non_string_cookie_names(self):
        for name in (None, 42, True):
            with self.subTest(name=name), self.assertRaises(ValueError):
                SessionSettings(database_path=self.db, cookie_name=name)

    def test_secure_prefix_requires_secure_flag(self):
        for name in ("__Host-session", "__Secure-session"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                SessionSettings(database_path=self.db, cookie_name=name, secure=False)

    def test_valid_names_and_secure_prefixes(self):
        for name in ("aurora_session", "__Host-session", "__Secure-session", "app-session.v1"):
            with self.subTest(name=name):
                self.assertEqual(SessionSettings(database_path=self.db, cookie_name=name).cookie_name, name)
