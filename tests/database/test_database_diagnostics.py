import unittest
from aurora.database.cli_diagnostics import explain_plan
from aurora.database.plan import MigrationPlan, MigrationStep
from tests.fixtures.migration_models import MigrationExample


class DatabaseDiagnosticsTests(unittest.TestCase):
    def test_empty(self):
        self.assertIn('no changes required', explain_plan(MigrationPlan(()), [MigrationExample]))

    def test_candidate(self):
        plan = MigrationPlan((MigrationStep('missing_column', 'migration_example', 'title', None, None, 'review', 'Check defaults'),))
        output = explain_plan(plan, [MigrationExample])
        self.assertIn('[CANDIDATE]', output)
        self.assertIn('Nullable ADD COLUMN', output)
        self.assertIn('not an execution guarantee', output)

    def test_blocked(self):
        plan = MigrationPlan((MigrationStep('type_affinity', 'migration_example', 'title', 'INTEGER', 'TEXT', 'high', 'Check types'),))
        output = explain_plan(plan, [MigrationExample])
        self.assertIn('[BLOCKED]', output)
        self.assertIn('Manual schema review', output)

    def test_unknown_fails_closed(self):
        plan = MigrationPlan((MigrationStep('future_operation', 'migration_example', None, None, None, 'high', 'Unknown'),))
        self.assertIn('[BLOCKED]', explain_plan(plan, [MigrationExample]))
