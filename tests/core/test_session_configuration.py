"""Compatibility checks for concise session configuration."""
import tempfile
import unittest
from pathlib import Path
from aurora import Aurora
from aurora.security.sessions import SessionSettings


class TestSessionConfiguration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SessionSettings(database_path=str(Path(self.tmp.name) / "sessions.sqlite3"), secure=False)

    def test_concise_settings_enable_sessions(self):
        app = Aurora(sessions=self.settings)
        self.assertIsNotNone(app.session_manager)

    def test_legacy_configuration_still_works(self):
        app = Aurora(sessions=True, session_settings=self.settings)
        self.assertIsNotNone(app.session_manager)

    def test_sessions_remain_opt_in(self):
        self.assertIsNone(Aurora().session_manager)

    def test_reject_duplicate_configuration(self):
        with self.assertRaises(ValueError):
            Aurora(sessions=self.settings, session_settings=self.settings)

    def test_reject_invalid_sessions_argument(self):
        with self.assertRaises(TypeError):
            Aurora(sessions="enabled")
