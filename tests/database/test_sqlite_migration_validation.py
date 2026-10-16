import sqlite3
from contextlib import closing
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from aurora.database.column import column
from aurora.database.model import Model
from aurora.database.plan import MigrationPlan, MigrationStep
from aurora.database.migration_operations import review_operations
from aurora.database.migration_validation import validate_sqlite_operations


class SQLiteOperationValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Item6613(Model):
            id = column(int, primary_key=True)
            title = column(str, nullable=True)
            required = column(str)
        class New6613(Model):
            id = column(int, primary_key=True)
        cls.item, cls.new = Item6613, New6613

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "db.sqlite3"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE item6613 (id INTEGER PRIMARY KEY)")
            db.execute("INSERT INTO item6613 VALUES (1)")

    def check(self, kind, table="item6613", col=None):
        step = MigrationStep(kind, table, col, None, None, "review", "Review")
        review = review_operations(MigrationPlan((step,)), [self.item, self.new])
        return validate_sqlite_operations(self.path, review, [self.item, self.new])

    def test_nullable_column_candidate_still_nonexecutable(self):
        report = self.check("missing_column", col="title")
        self.assertEqual(report.results[0].status, "eligible_for_further_review")
        self.assertFalse(report.executable)
        self.assertFalse(report.results[0].executable)

    def test_existing_column_detected(self):
        self.assertEqual(self.check("missing_column", col="id").results[0].status, "blocked")

    def test_required_column_blocked(self):
        self.assertEqual(self.check("missing_column", col="required").results[0].status, "blocked")

    def test_new_table_candidate(self):
        self.assertEqual(self.check("missing_table", table="new6613").results[0].status, "eligible_for_further_review")

    def test_existing_table_drift_blocked(self):
        self.assertEqual(self.check("missing_table").results[0].status, "blocked")

    def test_unsupported_difference_blocked(self):
        report = self.check("type_affinity", col="id")
        self.assertTrue(report.has_blockers)
        self.assertIn("NON-EXECUTABLE", report.format_text())

    def test_missing_database_fails_without_creation(self):
        missing = self.path.with_name("missing.sqlite3")
        with self.assertRaises(FileNotFoundError):
            validate_sqlite_operations(missing, review_operations(MigrationPlan(()), [self.item]), [self.item])
        self.assertFalse(missing.exists())

    def test_immutable_results(self):
        result = self.check("missing_table", table="new6613").results[0]
        with self.assertRaises(FrozenInstanceError):
            result.status = "approved"

    def test_no_changes_to_source(self):
        before = self.path.read_bytes()
        self.check("missing_column", col="title")
        self.assertEqual(before, self.path.read_bytes())

    def test_invalid_review(self):
        with self.assertRaises(TypeError):
            validate_sqlite_operations(self.path, object(), [self.item])
