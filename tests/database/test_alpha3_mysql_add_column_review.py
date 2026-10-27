"""No-write tests for MySQL nullable ADD COLUMN review preparation."""
import unittest

from aurora.database.mysql_add_column_review import (
    MySQLAddColumnProposal, prepare_mysql_nullable_add_column,
)


class TestMySQLAddColumnReview(unittest.TestCase):
    def setUp(self):
        self.base = dict(database_alias="main", schema_name="scratch",
                         table_name="d4_mysql_init_probe",
                         model_reference="tests.fixtures.mysql_init_probe:MySQLInitProbe")

    def proposal(self, **changes):
        return MySQLAddColumnProposal(**(self.base | changes))

    def test_compiles_deterministic_nullable_column_without_execution(self):
        proposal = MySQLAddColumnProposal(
            database_alias="main", schema_name="scratch",
            table_name="a3_nullable_column_probe",
            model_reference="tests.fixtures.mysql_add_column_probe:MySQLAddColumnProbe",
            column_name="description",
        )
        sql, digest = prepare_mysql_nullable_add_column(proposal)
        self.assertEqual(sql, "ALTER TABLE `a3_nullable_column_probe` ADD COLUMN `description` VARCHAR(120) NULL")
        self.assertEqual(len(digest), 64)
        self.assertEqual((sql, digest), prepare_mysql_nullable_add_column(proposal))

    def test_rejects_required_column(self):
        with self.assertRaises(ValueError):
            prepare_mysql_nullable_add_column(self.proposal(column_name="title"))

    def test_rejects_primary_key(self):
        with self.assertRaises(ValueError):
            prepare_mysql_nullable_add_column(self.proposal(column_name="id"))

    def test_rejects_unknown_column(self):
        with self.assertRaises(ValueError):
            prepare_mysql_nullable_add_column(self.proposal(column_name="missing"))

    def test_rejects_wrong_table(self):
        with self.assertRaises(ValueError):
            prepare_mysql_nullable_add_column(self.proposal(table_name="other", column_name="title"))

    def test_rejects_identifier_injection(self):
        with self.assertRaises(ValueError):
            self.proposal(column_name="x`; DROP TABLE users; --")

    def test_rejects_invalid_model_reference(self):
        with self.assertRaises(ValueError):
            self.proposal(model_reference="os:system()", column_name="title")

    def test_requires_proposal_object(self):
        with self.assertRaises(TypeError):
            prepare_mysql_nullable_add_column(None)


if __name__ == "__main__":
    unittest.main()
