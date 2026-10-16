"""Read-only constraint comparison tests for Phase 6.6.3."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from aurora.database import Model, column
from aurora.database.compare import compare_sqlite


class SchemaConstraintTests(unittest.TestCase):
    def check(self, ddl, models):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite3'
            with closing(sqlite3.connect(path)) as conn:
                for statement in ddl:
                    conn.execute(statement)
                conn.commit()
            return compare_sqlite(path, models)

    def test_matching_foreign_key(self):
        class Parent663(Model):
            id = column(int, primary_key=True)
        class Child663(Model):
            id = column(int, primary_key=True)
            parent_id = column(int, references='parent663.id')
        report = self.check([
            'CREATE TABLE parent663 (id INTEGER PRIMARY KEY)',
            'CREATE TABLE child663 (id INTEGER PRIMARY KEY, parent_id INTEGER NOT NULL REFERENCES parent663(id))',
        ], [Parent663, Child663])
        self.assertTrue(report.matches, report.differences)

    def test_missing_and_wrong_foreign_key(self):
        class Link663(Model):
            id = column(int, primary_key=True)
            target_id = column(int, references='target663.id')
        missing = self.check(['CREATE TABLE link663 (id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL)'], [Link663])
        wrong = self.check(['CREATE TABLE link663 (id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL REFERENCES other(id))'], [Link663])
        self.assertIn('foreign_key', [d.kind for d in missing.differences])
        self.assertIn('foreign_key', [d.kind for d in wrong.differences])

    def test_unique_semantics(self):
        class Unique663(Model):
            id = column(int, primary_key=True)
            name = column(str, unique=True)
        match = self.check(['CREATE TABLE unique663 (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL UNIQUE)'], [Unique663])
        missing = self.check(['CREATE TABLE unique663 (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)'], [Unique663])
        self.assertTrue(match.matches, match.differences)
        self.assertIn('unique', [d.kind for d in missing.differences])

    def test_unexpected_unique(self):
        class Plain663(Model):
            id = column(int, primary_key=True)
            label = column(str)
        report = self.check(['CREATE TABLE plain663 (id INTEGER PRIMARY KEY, label VARCHAR NOT NULL UNIQUE)'], [Plain663])
        self.assertIn('unique', [d.kind for d in report.differences])

    def test_python_default_is_not_server_default(self):
        class Default663(Model):
            id = column(int, primary_key=True)
            status = column(str, default='new')
        match = self.check(['CREATE TABLE default663 (id INTEGER PRIMARY KEY, status VARCHAR NOT NULL)'], [Default663])
        mismatch = self.check(["CREATE TABLE default663 (id INTEGER PRIMARY KEY, status VARCHAR NOT NULL DEFAULT 'new')"], [Default663])
        self.assertTrue(match.matches, match.differences)
        self.assertIn('server_default', [d.kind for d in mismatch.differences])


if __name__ == '__main__':
    unittest.main()
