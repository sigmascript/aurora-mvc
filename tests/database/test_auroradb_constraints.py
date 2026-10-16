"""Regression tests for simple Aurora-facing database constraints."""

import unittest

from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from aurora.database import Database, Model, column


class TestAuroraConstraints(unittest.TestCase):
    def test_unique_column_declares_unique_constraint(self):
        definition = column(str, unique=True)
        self.assertTrue(definition.unique)
        self.assertTrue(definition.to_sqlalchemy("username").unique)

    def test_references_declares_foreign_key(self):
        definition = column(int, references="users.id")
        self.assertEqual(definition.references, "users.id")
        sql_column = definition.to_sqlalchemy("user_id")
        self.assertEqual(next(iter(sql_column.foreign_keys)).target_fullname, "users.id")

    def test_invalid_reference_is_rejected(self):
        for value in ("", "users", ".id", "users.", 123):
            with self.subTest(value=value), self.assertRaises((TypeError, ValueError)):
                column(int, references=value)

    def test_unique_rejects_non_boolean(self):
        with self.assertRaises(TypeError):
            column(str, unique="yes")

    def test_database_enforces_unique_username(self):
        class ConstraintUser(Model):
            __table__ = "constraint_users"
            id = column(int, primary_key=True)
            username = column(str, unique=True)

        database = Database("sqlite:///:memory:")
        try:
            database.create_tables(Model.metadata)
            Model.bind(database)
            ConstraintUser.create(username="alice")
            with self.assertRaises(IntegrityError):
                ConstraintUser.create(username="alice")
            unique_constraints = inspect(database.engine).get_unique_constraints("constraint_users")
            self.assertTrue(any("username" in item["column_names"] for item in unique_constraints))
        finally:
            database.dispose()


if __name__ == "__main__":
    unittest.main()
