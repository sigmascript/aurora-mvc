"""Offline, no-write tests for restricted MySQL CREATE TABLE preparation."""
import unittest
from unittest.mock import Mock
from tests.fixtures.mysql_init_probe import MySQLInitProbe

from aurora.database.mysql_create_table_executor import (
    execute_mysql_create_table, prepare_mysql_create_table,
)
from aurora.database.mysql_execution_approval import MySQLApprovalRequest
from aurora.database.mysql_migration_contract import MySQLOperationContract
from aurora.database.mysql_migration_preflight import MySQLSchemaSnapshot


class TestMySQLCreateTableExecutor(unittest.TestCase):
    def setUp(self):
        self.operation = MySQLOperationContract(
            database_alias="main", schema_name="scratch", table_name="d4_mysql_init_probe",
            model_reference="tests.fixtures.mysql_init_probe:MySQLInitProbe",
        )

    def test_prepares_deterministic_mysql_ddl_without_execution(self):
        sql, digest = prepare_mysql_create_table(self.operation)
        self.assertIn("CREATE TABLE", sql)
        self.assertIn("d4_mysql_init_probe", sql)
        self.assertEqual(len(digest), 64)
        self.assertEqual((sql, digest), prepare_mysql_create_table(self.operation))

    def test_rejects_model_mismatch(self):
        wrong = MySQLOperationContract("main", "scratch", "other_table",
                "tests.fixtures.mysql_init_probe:MySQLInitProbe")
        with self.assertRaises(ValueError):
            prepare_mysql_create_table(wrong)

    def test_rejects_unconfirmed_execution_before_connecting(self):
        request = MySQLApprovalRequest(self.operation, "a" * 64, "b" * 64)
        reviewed = MySQLSchemaSnapshot("main", "scratch", ("d4_mysql_init_probe",), ())
        with self.assertRaises(PermissionError):
            execute_mysql_create_table(Mock(), request=request, reviewed=reviewed,
                                       approved_review_sha256=request.review_sha256(),
                                       approved_sql_sha256="a" * 64)

    def test_rejects_wrong_review_before_connecting(self):
        request = MySQLApprovalRequest(self.operation, "a" * 64, "b" * 64)
        reviewed = MySQLSchemaSnapshot("main", "scratch", ("d4_mysql_init_probe",), ())
        with self.assertRaises(PermissionError):
            execute_mysql_create_table(Mock(), request=request, reviewed=reviewed,
                                       approved_review_sha256="0" * 64,
                                       approved_sql_sha256="a" * 64, confirm=True)
