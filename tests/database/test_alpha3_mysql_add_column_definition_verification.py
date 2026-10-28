"""Post-DDL verification rejects ambiguous or incompatible reflected columns."""
from unittest.mock import Mock, patch

import pytest
from sqlalchemy.dialects.mysql import VARCHAR, INTEGER

from aurora.database.mysql_add_column_executor import _verify_applied_column_definition
from aurora.database.mysql_add_column_review import MySQLAddColumnProposal


def _proposal():
    return MySQLAddColumnProposal(
        "main", "sandbox", "a3_nullable_column_probe",
        "tests.fixtures.mysql_add_column_probe:MySQLAddColumnProbe", "description",
    )


@pytest.mark.parametrize("columns", [
    [],
    [{"name": "description", "type": VARCHAR(120), "nullable": True}] * 2,
    [{"name": "description", "type": VARCHAR(100), "nullable": True}],
    [{"name": "description", "type": INTEGER(), "nullable": True}],
    [{"name": "description", "type": VARCHAR(120), "nullable": False}],
])
def test_rejects_incorrect_definition(columns):
    with patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = columns
        with pytest.raises(RuntimeError):
            _verify_applied_column_definition(Mock(), _proposal())


def test_accepts_exact_nullable_column_definition():
    with patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [
            {"name": "description", "type": VARCHAR(120), "nullable": True}
        ]
        _verify_applied_column_definition(Mock(), _proposal())
        inspector.return_value.get_columns.assert_called_once_with(
            "a3_nullable_column_probe", schema="sandbox"
        )


@pytest.mark.parametrize("collation", ["utf8mb4_unicode_ci", "utf8mb4_0900_ai_ci"])
def test_accepts_inherited_mysql_varchar_collation(collation):
    with patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [
            {"name": "description", "type": VARCHAR(120, collation=collation), "nullable": True}
        ]
        _verify_applied_column_definition(Mock(), _proposal())


def test_rejects_wrong_length_even_with_inherited_collation():
    with patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [
            {"name": "description", "type": VARCHAR(121, collation="utf8mb4_unicode_ci"), "nullable": True}
        ]
        with pytest.raises(RuntimeError, match="differs"):
            _verify_applied_column_definition(Mock(), _proposal())


def test_rejects_nonnullable_column_even_with_inherited_collation():
    with patch("aurora.database.mysql_add_column_executor.inspect") as inspector:
        inspector.return_value.get_columns.return_value = [
            {"name": "description", "type": VARCHAR(120, collation="utf8mb4_unicode_ci"), "nullable": False}
        ]
        with pytest.raises(RuntimeError, match="differs"):
            _verify_applied_column_definition(Mock(), _proposal())
