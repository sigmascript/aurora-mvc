"""Contracts for the separately owned additive audit journal."""

import pytest

from aurora.database import (
    AdditiveEntry,
    initialize_additive_history,
    list_additive_history,
    migration_execution,
    migration_journal,
    schema_inspection,
)


def test_additive_journal_exports_are_identical():
    assert AdditiveEntry is migration_journal.AdditiveEntry
    assert initialize_additive_history is migration_journal.initialize_additive_history
    assert list_additive_history is migration_journal.list_additive_history
    assert migration_execution.AdditiveEntry is AdditiveEntry
    assert (
        migration_execution._verify_additive_journal is migration_journal._verify_additive_journal
    )
    assert migration_execution.initialize_additive_history is initialize_additive_history
    assert migration_execution.list_additive_history is list_additive_history


def test_additive_journal_initialization_requires_explicit_confirmation():
    class NoDatabaseCalls:
        def __getattr__(self, key):
            raise AssertionError(f"Unexpected database call: {key}")

    with pytest.raises(PermissionError, match="Explicit confirmation"):
        initialize_additive_history(NoDatabaseCalls())


def test_additive_journal_read_refuses_missing_structure_before_query():
    class MissingTable:
        dialect = "sqlite"

        def fetch_first(self, *args, **kwargs):
            raise AssertionError("Unexpected read of an absent journal")

    # Verify fail-closed behavior using the project's actual schema inspection.
    from unittest.mock import patch

    with patch.object(schema_inspection, "inspect_table") as inspect:
        inspect.return_value.exists = False
        inspect.return_value.columns = ()
        with pytest.raises(RuntimeError, match="journal missing"):
            list_additive_history(MissingTable())


def test_reflected_type_normalization_shared_with_execution():
    assert migration_execution._reflected_type_name is schema_inspection._reflected_type_name
    assert (
        schema_inspection._reflected_type_name("VARCHAR(36)", "postgresql")
        == "character varying(36)"
    )
    assert schema_inspection._reflected_type_name("DECIMAL(4,2)", "postgresql") == "numeric(4,2)"
    assert schema_inspection._reflected_type_name("VARCHAR(36)", "mysql") == "varchar(36)"
