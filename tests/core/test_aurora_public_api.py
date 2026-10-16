"""Public Aurora naming and compatibility tests."""
import unittest

from aurora import Application, Aurora, Controller, View, redirect


class AuroraPublicAPITests(unittest.TestCase):
    def test_aurora_aliases_application(self):
        self.assertIs(Aurora, Application)

    def test_minimal_aurora_initialization(self):
        self.assertIsInstance(Aurora(), Application)

    def test_existing_public_api_remains_available(self):
        self.assertTrue(all((Controller, View, redirect)))


if __name__ == "__main__":
    unittest.main()
