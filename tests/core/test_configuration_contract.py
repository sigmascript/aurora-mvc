"""Regression tests for private application configuration helpers."""

import unittest

from aurora.core.application import Application
from aurora.core.configuration import (
    validate_locales,
    validate_session_settings,
    validate_static_settings,
)


class ConfigurationExtractionTests(unittest.TestCase):
    def test_disabled_sessions_are_unchanged(self):
        self.assertIsNone(validate_session_settings(False, None))
        self.assertIsNone(Application().session_manager)

    def test_locales_are_normalized_to_tuple(self):
        self.assertEqual(validate_locales(["en", "ku"], "en"), (("en", "ku"), "en"))

    def test_static_settings_preserve_directory_alias(self):
        prefix, directory = validate_static_settings(".", None, "/assets")
        self.assertEqual(prefix, "/assets")
        self.assertTrue(directory.is_absolute())

    def test_invalid_static_prefix_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "static_url"):
            Application(static_url="relative")


if __name__ == "__main__":
    unittest.main()
