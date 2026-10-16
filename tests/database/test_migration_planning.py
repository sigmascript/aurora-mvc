"""Phase 6.6.4: advisory plans never execute or generate migration SQL."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from aurora.database import Model, column
from aurora.database.compare import SchemaComparison, SchemaDifference
from aurora.database.plan import plan_comparison, plan_schema, plan_sqlite
from aurora.database.schema import SchemaInfo


class MigrationPlanningTests(unittest.TestCase):
    def test_empty_plan(self):
        plan = plan_comparison(SchemaComparison(()))
        self.assertTrue(plan.is_empty)
        self.assertFalse(plan.requires_review)
        self.assertFalse(plan.executable)
        self.assertIn('No migration needed', plan.format_text())

    def test_missing_table_needs_review(self):
        class PlanNew664(Model):
            id = column(int, primary_key=True)
        plan = plan_schema(SchemaInfo(()), [PlanNew664])
        self.assertEqual(plan.steps[0].kind, 'missing_table')
        self.assertEqual(plan.steps[0].risk, 'review')
        self.assertFalse(plan.executable)

    def test_high_risk_differences(self):
        kinds = ('primary_key', 'nullable', 'foreign_key', 'unique', 'unmanaged_table', 'type_affinity')
        plan = plan_comparison(SchemaComparison(tuple(SchemaDifference(k, 'notes') for k in kinds)))
        self.assertEqual([step.risk for step in plan.steps], ['high'] * len(kinds))

    def test_unknown_difference_fails_closed(self):
        plan = plan_comparison(SchemaComparison((SchemaDifference('future_kind', 'notes'),)))
        self.assertEqual(plan.steps[0].risk, 'high')
        self.assertIn('manual investigation', plan.steps[0].recommendation)

    def test_invalid_input_rejected(self):
        with self.assertRaises(TypeError):
            plan_comparison(None)
        with self.assertRaises(TypeError):
            plan_comparison(SchemaComparison((None,)))

    def test_unmanaged_is_included_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE old_data (id INTEGER PRIMARY KEY, value TEXT)')
                db.commit()
            plan = plan_sqlite(path, [])
            self.assertEqual(plan.steps[0].kind, 'unmanaged_table')
            self.assertEqual(plan.steps[0].risk, 'high')

    def test_plan_does_not_change_database(self):
        class PlanNotes664(Model):
            id = column(int, primary_key=True)
            title = column(str, length=100)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE plan_notes664 (id INTEGER PRIMARY KEY)')
                db.execute('INSERT INTO plan_notes664 VALUES (1)')
                db.commit()
            before = path.read_bytes()
            plan = plan_sqlite(path, [PlanNotes664])
            self.assertEqual(path.read_bytes(), before)
            self.assertIn('missing_column', [step.kind for step in plan.steps])
            self.assertIn('NOT EXECUTABLE', plan.format_text())
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM plan_notes664').fetchone()[0], 1)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'missing.sqlite3'
            with self.assertRaises(FileNotFoundError):
                plan_sqlite(path, [])
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
