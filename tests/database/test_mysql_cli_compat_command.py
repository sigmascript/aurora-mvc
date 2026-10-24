"""CLI command dispatch and exit status for read-only MySQL compatibility."""
import contextlib
import io
import unittest
from unittest.mock import patch

from aurora.cli import main


class MySQLCompatibilityCommandTests(unittest.TestCase):
    def test_cli_reports_unverified_exit_three(self):
        with patch('aurora.database.cli_inspection.load_cli_database_config', return_value=(object(), object())), \
             patch('aurora.database.cli_mysql_compat.check_mysql_compatibility', return_value=('UNVERIFIED', (), ('unsupported property',))):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = main(['db', 'compat', '--database', 'analytics', '--model', 'tests.fixtures.mysql_init_probe:MySQLInitProbe'])
        self.assertEqual(result, 3)
        self.assertIn('Schema compatibility: UNVERIFIED', output.getvalue())


if __name__ == '__main__':
    unittest.main()
