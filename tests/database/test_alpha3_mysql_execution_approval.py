"""Tests for read-only Alpha 3 MySQL approval request identities."""
import unittest

from aurora.database.mysql_execution_approval import (
    MySQLApprovalRequest,
    require_mysql_executor_disabled,
)
from aurora.database.mysql_migration_contract import MySQLOperationContract


class MySQLApprovalRequestTests(unittest.TestCase):
    def operation(self, table_name="example"):
        return MySQLOperationContract(
            database_alias="main",
            schema_name="disposable",
            table_name=table_name,
            model_reference="example.models:Example",
        )

    def request(self, **changes):
        values = dict(
            operation=self.operation(),
            model_schema_sha256="a" * 64,
            database_state_sha256="b" * 64,
        )
        values.update(changes)
        return MySQLApprovalRequest(**values)

    def test_review_identity_is_stable_but_not_executable(self):
        request = self.request()
        self.assertEqual(request.review_sha256(), self.request().review_sha256())
        self.assertFalse(request.as_dict()["executable"])
        with self.assertRaisesRegex(RuntimeError, "disabled"):
            require_mysql_executor_disabled(request)

    def test_identity_changes_with_model_schema_database_or_operation(self):
        original = self.request().review_sha256()
        for change in (
            {"model_schema_sha256": "c" * 64},
            {"database_state_sha256": "d" * 64},
            {"operation": self.operation("another")},
        ):
            with self.subTest(change=change):
                self.assertNotEqual(original, self.request(**change).review_sha256())

    def test_rejects_invalid_inputs_and_unapproved_policy(self):
        for change in (
            {"operation": {}},
            {"model_schema_sha256": "not-a-digest"},
            {"database_state_sha256": "A" * 64},
            {"policy_version": "mysql-drop-table-v1"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.request(**change)
        with self.assertRaises(ValueError):
            require_mysql_executor_disabled({})


if __name__ == "__main__":
    unittest.main()
