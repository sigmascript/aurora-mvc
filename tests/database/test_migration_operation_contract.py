import unittest
from dataclasses import FrozenInstanceError
from aurora.database.column import column
from aurora.database.model import Model
from aurora.database.plan import MigrationPlan, MigrationStep
from aurora.database.migration_operations import review_operations, MigrationOperation

class OperationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Safe6612(Model):
            id = column(int, primary_key=True)
            extra = column(str, nullable=True)
            required = column(str, default="python only")
            owner = column(int, references="users.id")
            email = column(str, unique=True)
        cls.model = Safe6612

    def step(self, kind, column_name=None, table="safe6612"):
        return MigrationStep(kind, table, column_name, None, None, "review", "Review")

    def review(self, *steps):
        return review_operations(MigrationPlan(tuple(steps)), [self.model])

    def test_empty_never_executable(self):
        r = self.review()
        self.assertFalse(r.executable)
        self.assertFalse(r.has_blockers)
        self.assertIn("disabled", r.format_text())

    def test_missing_table_candidate_not_executable(self):
        r = self.review(self.step("missing_table"))
        self.assertEqual(r.operations[0].classification, "candidate")
        self.assertFalse(r.operations[0].executable)

    def test_nullable_column_candidate(self):
        r = self.review(self.step("missing_column", "extra"))
        self.assertEqual(r.operations[0].classification, "candidate")

    def test_python_default_does_not_make_not_null_safe(self):
        r = self.review(self.step("missing_column", "required"))
        self.assertEqual(r.operations[0].classification, "manual")

    def test_foreign_key_and_unique_require_review(self):
        r = self.review(self.step("missing_column", "owner"), self.step("missing_column", "email"))
        self.assertTrue(r.has_blockers)
        self.assertEqual([x.classification for x in r.operations], ["manual", "manual"])

    def test_destructive_differences_unsupported(self):
        r = self.review(self.step("type_affinity", "extra"), self.step("unmanaged_table"))
        self.assertEqual([x.classification for x in r.operations], ["unsupported", "unsupported"])

    def test_unknown_model_fails_closed(self):
        r = self.review(self.step("missing_column", "extra", "other_table"))
        self.assertEqual(r.operations[0].classification, "unsupported")

    def test_invalid_inputs(self):
        with self.assertRaises(TypeError): review_operations("not a plan", [self.model])
        with self.assertRaises(TypeError): review_operations(MigrationPlan(()), [object])
        with self.assertRaises(ValueError): review_operations(MigrationPlan(()), [self.model, self.model])

    def test_immutable_contract_and_no_sql(self):
        op = self.review(self.step("missing_table")).operations[0]
        with self.assertRaises(FrozenInstanceError): op.classification = "approved"
        self.assertNotIn("CREATE TABLE safe6612", self.review(self.step("missing_table")).format_text())
        self.assertFalse(hasattr(op, "execute"))

    def test_invalid_classification_rejected(self):
        with self.assertRaises(ValueError): MigrationOperation("x", "t", None, "approved", "reason")
