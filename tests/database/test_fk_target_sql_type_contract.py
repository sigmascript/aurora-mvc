"""Preflight rejects incompatible declared FK SQL types without executing DDL."""

import pytest

from aurora.database import Model, column
from aurora.database.init_validation import validate_init_models


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_matching_explicit_varchar_fk_is_accepted(dialect):
    class Parent(Model):
        __table__ = "e2_sql_parent"
        code = column("VARCHAR(32)", unique=True)

    class Child(Model):
        __table__ = "e2_sql_child"
        parent_code = column("VARCHAR(32)", reference="e2_sql_parent.code")

    assert validate_init_models((Child, Parent), dialect=dialect) == (Parent, Child)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_conflicting_explicit_decimal_fk_fails_before_connection(dialect):
    class Parent(Model):
        __table__ = "e2_decimal_parent"
        code = column("DECIMAL(12,2)", unique=True)

    class Child(Model):
        __table__ = "e2_decimal_child"
        parent_code = column("DECIMAL(12,3)", reference="e2_decimal_parent.code")

    with pytest.raises(ValueError, match="SQL column declarations"):
        validate_init_models((Parent, Child), dialect=dialect)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_equivalent_explicit_text_and_implicit_text_are_compatible(dialect):
    class Parent(Model):
        __table__ = "e2_mixed_parent"
        code = column("TEXT", unique=True, length=None)

    class Child(Model):
        __table__ = "e2_mixed_child"
        parent_code = column(str, reference="e2_mixed_parent.code")

    if dialect == "mysql":
        with pytest.raises(ValueError, match="Unique MySQL string"):
            validate_init_models((Child, Parent), dialect=dialect)
    else:
        assert validate_init_models((Child, Parent), dialect=dialect) == (Parent, Child)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_plain_integer_fk_remains_compatible(dialect):
    class Parent(Model):
        __table__ = "e2_plain_parent"
        id = column(int, primary_key=True)

    class Child(Model):
        __table__ = "e2_plain_child"
        parent_id = column(int, reference="e2_plain_parent.id")

    assert validate_init_models((Child, Parent), dialect=dialect) == (Parent, Child)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_mismatched_length_conventions_fail(dialect):
    class Parent(Model):
        __table__ = "e2_length_parent"
        code = column(str, length=32, unique=True)

    class Child(Model):
        __table__ = "e2_length_child"
        parent_code = column(str, length=24, reference="e2_length_parent.code")

    with pytest.raises(ValueError, match="SQL column declarations"):
        validate_init_models((Parent, Child), dialect=dialect)
