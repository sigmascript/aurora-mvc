import unittest
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    Integer,
    String,
)

from aurora.database import (
    Column,
    column,
)


class TestColumn(unittest.TestCase):

    def test_column_helper_creates_column(self):
        definition = column(str)

        self.assertIsInstance(
            definition,
            Column,
        )

        self.assertIs(
            definition.python_type,
            str,
        )

        self.assertFalse(
            definition.primary_key,
        )

        self.assertFalse(
            definition.nullable,
        )

    def test_column_supports_primary_key(self):
        definition = column(
            int,
            primary_key=True,
        )

        self.assertTrue(
            definition.primary_key,
        )

    def test_column_supports_nullable(self):
        definition = column(
            str,
            nullable=True,
        )

        self.assertTrue(
            definition.nullable,
        )

    def test_column_supports_default(self):
        definition = column(
            bool,
            default=False,
        )

        self.assertIs(
            definition.default,
            False,
        )

    def test_string_column_supports_length(self):
        definition = column(
            str,
            length=100,
        )

        self.assertEqual(
            definition.length,
            100,
        )

    def test_unsupported_type_is_rejected(self):
        with self.assertRaises(TypeError):
            column(list)

    def test_length_is_rejected_for_non_string(self):
        with self.assertRaises(TypeError):
            column(
                int,
                length=100,
            )

    def test_invalid_string_length_is_rejected(self):
        invalid_lengths = [
            0,
            -1,
            1.5,
            True,
        ]

        for length in invalid_lengths:
            with self.subTest(
                length=length
            ):
                with self.assertRaises(
                    ValueError
                ):
                    column(
                        str,
                        length=length,
                    )

    def test_python_types_map_to_sqlalchemy_types(self):
        mappings = [
            (str, String),
            (int, Integer),
            (float, Float),
            (bool, Boolean),
            (datetime, DateTime),
            (date, Date),
        ]

        for python_type, expected_type in mappings:
            with self.subTest(
                python_type=python_type
            ):
                definition = column(
                    python_type
                )

                sql_column = (
                    definition.to_sqlalchemy(
                        "value"
                    )
                )

                self.assertIsInstance(
                    sql_column.type,
                    expected_type,
                )

    def test_sqlalchemy_column_preserves_options(self):
        definition = column(
            str,
            primary_key=True,
            nullable=False,
            default="Aurora",
            length=100,
        )

        sql_column = (
            definition.to_sqlalchemy(
                "name"
            )
        )

        self.assertEqual(
            sql_column.name,
            "name",
        )

        self.assertTrue(
            sql_column.primary_key,
        )

        self.assertFalse(
            sql_column.nullable,
        )

        self.assertEqual(
            sql_column.type.length,
            100,
        )

        self.assertIsNotNone(
            sql_column.default,
        )

        self.assertEqual(
            sql_column.default.arg,
            "Aurora",
        )


if __name__ == "__main__":
    unittest.main()
