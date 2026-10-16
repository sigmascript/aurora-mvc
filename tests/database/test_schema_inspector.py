from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aurora.database.schema import inspect_sqlite


class SchemaInspectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'sample.sqlite3'
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript('''CREATE TABLE authors(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE posts(id INTEGER PRIMARY KEY, author_id INTEGER REFERENCES authors(id),
                   title TEXT NOT NULL DEFAULT 'Untitled');
CREATE INDEX ix_posts_title ON posts(title);
INSERT INTO authors(name) VALUES ('Existing data');
''')

    def test_tables_columns_and_defaults(self):
        schema = inspect_sqlite(self.db)
        self.assertEqual([t.name for t in schema.tables], ['authors', 'posts'])
        posts = schema.tables[1]
        self.assertEqual(posts.columns[2].name, 'title')
        self.assertEqual(posts.columns[2].default, "'Untitled'")
        self.assertFalse(posts.columns[2].nullable)

    def test_indexes_and_foreign_keys(self):
        posts = inspect_sqlite(self.db).tables[1]
        self.assertEqual(posts.indexes[0].columns, ('title',))
        self.assertEqual(posts.foreign_keys[0].referenced_table, 'authors')

    def test_does_not_modify_existing_data(self):
        before = self.db.read_bytes()
        inspect_sqlite(self.db)
        self.assertEqual(self.db.read_bytes(), before)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute('SELECT name FROM authors').fetchone()[0], 'Existing data')

    def test_missing_database_does_not_create_file(self):
        missing = Path(self.temp.name) / 'missing.db'
        with self.assertRaises(FileNotFoundError):
            inspect_sqlite(missing)
        self.assertFalse(missing.exists())

    def test_empty_database(self):
        empty = Path(self.temp.name) / 'empty.db'
        sqlite3.connect(empty).close()
        self.assertEqual(inspect_sqlite(empty).tables, ())
