import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from aurora.database import Model, column
from aurora.database.compare import compare_sqlite, compare_schema
from aurora.database.schema import SchemaInfo


class SchemaComparisonTests(unittest.TestCase):
    def test_missing_table(self):
        class MissingTable662(Model):
            id = column(int, primary_key=True)
        report = compare_schema(SchemaInfo(()), [MissingTable662])
        self.assertEqual(report.differences[0].kind, 'missing_table')

    def test_existing_table_and_missing_column(self):
        class CompareEntry662(Model):
            id = column(int, primary_key=True)
            title = column(str, length=100)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE compare_entry662 (id INTEGER PRIMARY KEY)')
                conn.commit()
            report = compare_sqlite(path, [CompareEntry662])
            self.assertIn('missing_column', [d.kind for d in report.differences])

    def test_matching_schema(self):
        class CompareMatch662(Model):
            id = column(int, primary_key=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE compare_match662 (id INTEGER PRIMARY KEY)')
                conn.commit()
            self.assertTrue(compare_sqlite(path, [CompareMatch662]).matches)

    def test_unmanaged_objects_are_opt_in(self):
        class CompareOwned662(Model):
            id = column(int, primary_key=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'test.db'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE compare_owned662 (id INTEGER PRIMARY KEY, extra TEXT)')
                conn.execute('CREATE TABLE legacy (id INTEGER)')
                conn.commit()
            self.assertTrue(compare_sqlite(path, [CompareOwned662]).matches)
            kinds = {d.kind for d in compare_sqlite(path, [CompareOwned662], include_unmanaged=True).differences}
            self.assertEqual(kinds, {'unmanaged_column', 'unmanaged_table'})

    def test_invalid_model_and_duplicate(self):
        class CompareDup662(Model):
            id = column(int, primary_key=True)
        with self.assertRaises(TypeError):
            compare_schema(SchemaInfo(()), [object])
        with self.assertRaises(ValueError):
            compare_schema(SchemaInfo(()), [CompareDup662, CompareDup662])


if __name__ == '__main__':
    unittest.main()
