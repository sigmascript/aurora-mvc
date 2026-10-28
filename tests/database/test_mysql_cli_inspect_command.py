"""CLI wiring for MySQL metadata inspection and repeat-init guidance."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import main


class MySQLCLIInspectCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = Path(directory.name) / 'databases.json'
        self.config.write_text(json.dumps({'databases': {
            'default': {'url': 'sqlite:///app.sqlite3'},
            'analytics': {'url': 'mysql+pymysql://user:private_password@localhost/demo'},
        }}), encoding='utf-8')

    def test_plan_reports_metadata_limitations_without_compatibility_claim(self):
        report = {'database_alias': 'analytics', 'schema_status': 'UNVERIFIED',
                  'scope': 'selected models only', 'read_only': True,
                  'executable': False, 'items': []}
        output = io.StringIO()
        with patch('aurora.database.cli_mysql_plan.plan_mysql_schema', return_value=report), \
             contextlib.redirect_stdout(output):
            result = main(['db', 'plan', '--config', str(self.config),
                           '--database', 'analytics'])
        self.assertEqual(result, 0)
        self.assertIn('Unselected tables and some MySQL features are not verified.', output.getvalue())
        self.assertIn('Advisory only', output.getvalue())


if __name__ == '__main__':
    unittest.main()
