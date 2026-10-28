import argparse
import unittest
from unittest.mock import patch
from aurora.database.cli_unified_mysql_migrate import run_unified_mysql_migrate


class UnifiedMySQLMigrationTests(unittest.TestCase):
    def args(self, **changes):
        values = dict(config='db.json', database='default', schema='staging',
                      table='users', column=None, model=['models:User'],
                      check=False, explain=False, format='text', approve=None,
                      name=None, backup=None, output_dir='migrations',
                      apply=False, dry_run=True, approve_review=None,
                      approve_sql=None, confirm=None)
        values.update(changes)
        return argparse.Namespace(**values)

    def test_create_table_delegates_review(self):
        with patch('aurora.database.cli_mysql_create_table.run_mysql_create_table', return_value={'status':'REVIEW_ONLY'}) as run:
            self.assertEqual(run_unified_mysql_migrate(self.args())['status'], 'REVIEW_ONLY')
            self.assertFalse(run.call_args.kwargs['apply'])

    def test_add_column_delegates_review(self):
        with patch('aurora.database.cli_mysql_add_column.run_mysql_add_column', return_value={'status':'REVIEW_ONLY'}) as run:
            run_unified_mysql_migrate(self.args(column='email'))
            self.assertEqual(run.call_args.kwargs['column'], 'email')

    def test_apply_passes_approvals_unchanged(self):
        with patch('aurora.database.cli_mysql_add_column.run_mysql_add_column', return_value={'status':'APPLIED'}) as run:
            run_unified_mysql_migrate(self.args(column='email', dry_run=False, apply=True,
                approve_review='r', approve_sql='s', confirm='APPLY'))
            self.assertEqual(run.call_args.kwargs['approved_review'], 'r')
            self.assertTrue(run.call_args.kwargs['apply'])

    def test_rejects_ambiguous_or_unsupported(self):
        for changes in (dict(model=[]), dict(model=['a:A','b:B']), dict(table=None),
                        dict(schema=None), dict(apply=True), dict(dry_run=False),
                        dict(apply=True,dry_run=True), dict(check=True),
                        dict(approve_review='abc'), dict(backup='x.sqlite3')):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                run_unified_mysql_migrate(self.args(**changes))
