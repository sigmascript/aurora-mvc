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

    def test_inspect_reports_metadata_without_compatibility_claim(self):
        output = io.StringIO()
        with patch('aurora.database.cli_inspection.inspect_mysql_details',
                   return_value=(('records', 3, 1, 0),)) as inspect, \
             contextlib.redirect_stdout(output):
            result = main(['db', 'inspect', '--config', str(self.config),
                           '--database', 'analytics'])
        self.assertEqual(result, 0)
        self.assertIn('records: 3 column(s), 1 index(es), 0 foreign key(s)', output.getvalue())
        self.assertIn('model compatibility not verified', output.getvalue())
        inspect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
