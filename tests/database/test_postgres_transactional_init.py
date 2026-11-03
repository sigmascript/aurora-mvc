"""PostgreSQL guarded initialization contract (no server required)."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aurora.database.postgres_init import execute_postgres_init


def test_wrong_dialect_never_opens_transaction():
    connection = SimpleNamespace(dialect="sqlite", transaction=Mock())

    with pytest.raises(ValueError, match="PostgreSQL"):
        execute_postgres_init(connection, [object()], approval="a" * 64,
                              expected_approval="a" * 64)

    connection.transaction.assert_not_called()


def test_missing_approval_never_opens_transaction():
    connection = SimpleNamespace(dialect="postgresql", transaction=Mock())

    with pytest.raises(PermissionError, match="approval"):
        execute_postgres_init(connection, [object()], approval="wrong",
                              expected_approval="a" * 64)

    connection.transaction.assert_not_called()


def test_empty_model_set_refused():
    connection = SimpleNamespace(dialect="postgresql", transaction=Mock())

    with pytest.raises(ValueError, match="model"):
        execute_postgres_init(connection, [], approval="a" * 64,
                              expected_approval="a" * 64)

    connection.transaction.assert_not_called()
