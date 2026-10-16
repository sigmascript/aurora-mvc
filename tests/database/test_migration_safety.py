import unittest
from dataclasses import FrozenInstanceError

from aurora.database.migration_safety import (
    ExecutionSafetyPolicy, plan_digest, review_migration,
)
from aurora.database.plan import MigrationPlan, MigrationStep


class MigrationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.step = MigrationStep('missing_column', 'notes', 'archived', None,
                                  'BOOLEAN', 'review', 'Check defaults')
        self.plan = MigrationPlan((self.step,))

    def test_blocked_plan_is_never_executable(self):
        review = review_migration(self.plan)
        self.assertEqual(review.status, 'blocked')
        self.assertFalse(review.executable)
        self.assertTrue(review.blockers)

    def test_empty_plan_has_no_changes_but_is_not_executable(self):
        review = review_migration(MigrationPlan(()))
        self.assertEqual(review.status, 'no_changes')
        self.assertFalse(review.executable)
        self.assertEqual(review.blockers, ())

    def test_digest_is_deterministic(self):
        self.assertEqual(plan_digest(self.plan), plan_digest(MigrationPlan((self.step,))))
        self.assertEqual(len(plan_digest(self.plan)), 64)

    def test_digest_changes_when_plan_changes(self):
        different = MigrationPlan((MigrationStep('missing_column', 'notes', 'other',
                              None, 'BOOLEAN', 'review', 'Check defaults'),))
        self.assertNotEqual(plan_digest(self.plan), plan_digest(different))

    def test_policy_defaults_are_conservative(self):
        self.assertTrue(all(vars(ExecutionSafetyPolicy()).values()))

    def test_review_and_policy_are_immutable(self):
        review = review_migration(self.plan)
        with self.assertRaises(FrozenInstanceError):
            review.status = 'approved'
        with self.assertRaises(FrozenInstanceError):
            review.policy.require_verified_backup = False

    def test_review_report_explains_no_execution(self):
        report = review_migration(self.plan).format_text()
        self.assertIn('Execution disabled', report)
        self.assertIn('backup', report)
        self.assertIn('Plan SHA-256:', report)

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(TypeError):
            review_migration(object())
        with self.assertRaises(TypeError):
            plan_digest(object())
        with self.assertRaises(TypeError):
            review_migration(self.plan, policy=object())


if __name__ == '__main__':
    unittest.main()
