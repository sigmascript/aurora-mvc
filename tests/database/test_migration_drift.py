import unittest
from dataclasses import replace
from aurora.database.schema import SchemaInfo, TableInfo, ColumnInfo
from aurora.database.plan import MigrationPlan, MigrationStep, plan_schema
from aurora.database.migration_drift import (
    schema_digest, capture_baseline, check_schema_drift, SchemaBaseline,
)

class MigrationDriftTests(unittest.TestCase):
    def setUp(self):
        self.schema = SchemaInfo((TableInfo('items',
            (ColumnInfo('id', 'INTEGER', False, None, 1),), (), ()),))
        self.plan = MigrationPlan(())

    def test_stable_digest(self):
        self.assertEqual(schema_digest(self.schema), schema_digest(self.schema))

    def test_digest_changes_with_column(self):
        changed = SchemaInfo((replace(self.schema.tables[0], columns=(
            ColumnInfo('id', 'TEXT', False, None, 1),)),))
        self.assertNotEqual(schema_digest(self.schema), schema_digest(changed))

    def test_order_independent_tables(self):
        extra = TableInfo('other', (), (), ())
        a = SchemaInfo((extra, *self.schema.tables))
        b = SchemaInfo((*self.schema.tables, extra))
        self.assertEqual(schema_digest(a), schema_digest(b))

    def test_baseline_has_both_digests(self):
        baseline = capture_baseline(self.schema, self.plan)
        self.assertEqual(len(baseline.schema_sha256), 64)
        self.assertEqual(len(baseline.plan_sha256), 64)

    def test_match_is_never_executable(self):
        baseline = capture_baseline(self.schema, plan_schema(self.schema, [], include_unmanaged=True))
        result = check_schema_drift(baseline, self.schema, [], include_unmanaged=True)
        self.assertTrue(result.matches)
        self.assertFalse(result.executable)

    def test_detects_schema_change_even_if_plan_unchanged(self):
        baseline = capture_baseline(self.schema, plan_schema(self.schema, [], include_unmanaged=False))
        changed = SchemaInfo((replace(self.schema.tables[0], columns=(
            ColumnInfo('id', 'INTEGER', False, '0', 1),)),))
        result = check_schema_drift(baseline, changed, [], include_unmanaged=False)
        self.assertFalse(result.matches)
        self.assertIn('snapshot changed', result.format_text())

    def test_detects_plan_change(self):
        baseline = capture_baseline(self.schema, self.plan)
        result = check_schema_drift(baseline, self.schema, [], include_unmanaged=True)
        self.assertNotEqual(result.expected_plan_sha256, result.observed_plan_sha256)
        self.assertFalse(result.matches)

    def test_invalid_inputs(self):
        with self.assertRaises(TypeError):
            schema_digest(None)
        with self.assertRaises(TypeError):
            capture_baseline(self.schema, None)
        with self.assertRaises(TypeError):
            check_schema_drift(None, self.schema, [])

    def test_report_format(self):
        baseline = capture_baseline(self.schema, plan_schema(self.schema, [], include_unmanaged=True))
        result = check_schema_drift(baseline, self.schema, [], include_unmanaged=True)
        self.assertIn('Migration execution disabled', result.format_text())

if __name__ == '__main__':
    unittest.main()
