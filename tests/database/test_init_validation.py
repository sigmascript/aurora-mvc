"""Multi-model schema validation without database side effects."""

import pytest

from aurora.database import Model, column
from aurora.database.init_validation import validate_init_models


class Parent(Model):
    __table__ = "alpha9_parent"
    id = column(int, primary_key=True)


class Child(Model):
    __table__ = "alpha9_child"
    id = column(int, primary_key=True)
    parent_id = column(int, references="alpha9_parent.id")


def test_dependency_first_order():
    assert validate_init_models([Child, Parent]) == (Parent, Child)


def test_duplicate_table_rejected():
    with pytest.raises(ValueError, match="Duplicate"):
        validate_init_models([Parent, Parent])


def test_external_reference_rejected():
    with pytest.raises(ValueError, match="outside"):
        validate_init_models([Child])


def test_empty_model_set_rejected():
    with pytest.raises(ValueError, match="At least"):
        validate_init_models([])


def test_sql_default_refused():
    class Defaulted(Model):
        __table__ = "alpha9_defaulted"
        id = column(int, primary_key=True)
        title = column(str, server_default="test")

    with pytest.raises(ValueError, match="server defaults"):
        validate_init_models([Defaulted])


def test_cycle_refused():
    class First(Model):
        __table__ = "alpha9_first"
        id = column(int, primary_key=True)
        second_id = column(int, references="alpha9_second.id")

    class Second(Model):
        __table__ = "alpha9_second"
        id = column(int, primary_key=True)
        first_id = column(int, references="alpha9_first.id")

    with pytest.raises(ValueError, match="Cyclic"):
        validate_init_models([First, Second])


def test_invalid_fk_target_refused():
    class Bad(Model):
        __table__ = "alpha9_bad"
        id = column(int, primary_key=True)
        parent_id = column(int, references="alpha9_parent.missing")

    with pytest.raises(ValueError, match="target"):
        validate_init_models([Parent, Bad])
