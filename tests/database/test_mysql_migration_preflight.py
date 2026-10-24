"""Fail-closed structured MySQL preflight tests (no live database required)."""
import unittest

from aurora.database.mysql_migration_preflight import (
    MySQLSchemaSnapshot, derive_create_table_candidates,
    evaluate_mysql_outcome, validate_preflight_freshness,
)
from aurora.database.mysql_migration_contract import validate_execution_gate


class MySQLMigrationPreflightTests(unittest.TestCase):
    def snapshot(self, **kwargs):
        data = dict(database_alias="analytics", schema_name="aurora_alpha2_test",
                    selected_tables=("probe",), existing_tables=())
        data.update(kwargs)
        return MySQLSchemaSnapshot(**data)

    def candidate(self, snapshot=None):
        return derive_create_table_candidates(snapshot or self.snapshot(),
            {"tests.fixtures.mysql_init_probe:MySQLInitProbe": "probe"})[0]

    def test_candidate_is_structured_and_non_executable(self):
        candidate = self.candidate()
        self.assertEqual(candidate.table_name, "probe")
        self.assertFalse(candidate.as_dict()["executable"])
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            validate_execution_gate(operation=candidate, approval="approved",
                                    schema_revalidated=True, lock_acquired=True)

    def test_no_candidate_for_existing_table(self):
        snapshot = self.snapshot(existing_tables=("probe",))
        self.assertEqual(derive_create_table_candidates(snapshot,
            {"tests.fixtures.mysql_init_probe:MySQLInitProbe": "probe"}), ())

    def test_model_mapping_must_match_selection(self):
        with self.assertRaises(ValueError):
            derive_create_table_candidates(self.snapshot(), {"pkg:Model": "other"})
        with self.assertRaises(ValueError):
            derive_create_table_candidates(self.snapshot(), {})

    def test_lock_and_fresh_schema_required(self):
        snapshot = self.snapshot()
        candidate = self.candidate(snapshot)
        with self.assertRaisesRegex(RuntimeError, "lock"):
            validate_preflight_freshness(reviewed=snapshot, current=snapshot,
                                         candidate=candidate, advisory_lock_held=False)
        with self.assertRaisesRegex(RuntimeError, "changed"):
            validate_preflight_freshness(reviewed=snapshot,
                current=self.snapshot(existing_tables=("probe",)),
                candidate=candidate, advisory_lock_held=True)
        validate_preflight_freshness(reviewed=snapshot, current=snapshot,
                                     candidate=candidate, advisory_lock_held=True)

    def test_wrong_alias_is_rejected(self):
        snapshot = self.snapshot()
        other = self.snapshot(database_alias="other")
        with self.assertRaises(RuntimeError):
            validate_preflight_freshness(reviewed=snapshot, current=other,
                candidate=self.candidate(snapshot), advisory_lock_held=True)

    def test_snapshot_fingerprint_is_deterministic(self):
        self.assertEqual(self.snapshot().fingerprint(), self.snapshot().fingerprint())
        self.assertNotEqual(self.snapshot().fingerprint(),
                            self.snapshot(existing_tables=("probe",)).fingerprint())

    def test_partial_failure_outcomes_are_conservative(self):
        self.assertEqual(evaluate_mysql_outcome(ddl_attempted=True,
            table_exists_after=None, history_recorded=None), "OUTCOME_UNKNOWN")
        self.assertEqual(evaluate_mysql_outcome(ddl_attempted=True,
            table_exists_after=True, history_recorded=False), "DDL_PRESENT_HISTORY_UNCONFIRMED")
        self.assertEqual(evaluate_mysql_outcome(ddl_attempted=False,
            table_exists_after=None, history_recorded=None), "NOT_STARTED")
        with self.assertRaises(ValueError):
            evaluate_mysql_outcome(ddl_attempted=False, table_exists_after=True, history_recorded=None)


if __name__ == "__main__":
    unittest.main()
