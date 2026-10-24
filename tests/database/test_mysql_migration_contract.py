"""Tests for MySQL non-executable migration contracts."""
import unittest

from aurora.database.mysql_migration_contract import (
    MySQLOperationContract, MySQLOperationKind, MySQLExecutionState,
    classify_mysql_failure, validate_execution_gate,
)


class MySQLMigrationContractTests(unittest.TestCase):
    def candidate(self, **overrides):
        values = dict(database_alias="analytics", schema_name="aurora_alpha2_test",
                      table_name="d4_mysql_init_probe",
                      model_reference="tests.fixtures.mysql_init_probe:MySQLInitProbe")
        values.update(overrides)
        return MySQLOperationContract(**values)

    def test_contract_is_structured_and_never_executable(self):
        candidate = self.candidate()
        self.assertEqual(candidate.kind, MySQLOperationKind.CREATE_TABLE)
        self.assertFalse(candidate.as_dict()["executable"])
        self.assertEqual(candidate.identity_sha256(), self.candidate().identity_sha256())
        self.assertNotEqual(candidate.identity_sha256(), self.candidate(table_name="another_table").identity_sha256())

    def test_rejects_unsafe_identifiers(self):
        for field, value in (("table_name", "users; DROP TABLE users"),
                             ("schema_name", "db-name"), ("database_alias", ""),
                             ("table_name", "x" * 65),
                             ("model_reference", "package:Class:Other")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.candidate(**{field: value})

    def test_rejects_unsupported_operation(self):
        with self.assertRaises(ValueError):
            self.candidate(kind="DROP_TABLE")

    def test_gate_never_accepts_approval_or_flags(self):
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            validate_execution_gate(operation=self.candidate(), approval="anything",
                                    schema_revalidated=True, lock_acquired=True,
                                    backup_confirmed=True)
        with self.assertRaises(ValueError):
            validate_execution_gate(operation={})

    def test_partial_failure_states_are_conservative(self):
        self.assertEqual(classify_mysql_failure(ddl_started=False, ddl_confirmed=False,
                         history_confirmed=False), MySQLExecutionState.NOT_STARTED)
        self.assertEqual(classify_mysql_failure(ddl_started=True, ddl_confirmed=False,
                         history_confirmed=False), MySQLExecutionState.OUTCOME_UNKNOWN)
        self.assertEqual(classify_mysql_failure(ddl_started=True, ddl_confirmed=True,
                         history_confirmed=False), MySQLExecutionState.DDL_APPLIED_HISTORY_UNCONFIRMED)
        self.assertEqual(classify_mysql_failure(ddl_started=True, ddl_confirmed=True,
                         history_confirmed=True), MySQLExecutionState.VERIFIED_COMPLETE)
        with self.assertRaises(ValueError):
            classify_mysql_failure(ddl_started=False, ddl_confirmed=False, history_confirmed=True)


if __name__ == "__main__":
    unittest.main()
