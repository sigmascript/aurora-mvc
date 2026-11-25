"""Read-only FK inventory and action equivalence across server backends."""

import pytest

from aurora.database import Model, column
from aurora.database import schema_planning as planning
from aurora.database.schema_inspection import inspect_table


class Child(Model):
    __table__ = "children"
    id = column(int, primary_key=True)
    parent_id = column(int, reference="parents.id")


class FakeDatabase:
    def __init__(self, dialect, fk_rows):
        self.dialect = dialect
        self.fk_rows = fk_rows
        self.queries = []

    def fetch_first(self, sql, params=None):
        self.queries.append((sql, params))
        return {"name": "children"}

    def fetch_all(self, sql, params=None):
        self.queries.append((sql, params))
        if self.dialect == "mysql":
            if "information_schema.COLUMNS" in sql:
                return [
                    dict(
                        name="id",
                        sql_type="bigint",
                        nullable="NO",
                        column_key="PRI",
                        default_sql=None,
                    ),
                    dict(
                        name="parent_id",
                        sql_type="bigint",
                        nullable="NO",
                        column_key="",
                        default_sql=None,
                    ),
                ]
            if "information_schema.STATISTICS" in sql:
                return []
            if "REFERENTIAL_CONSTRAINTS" in sql:
                return self.fk_rows
        else:
            if sql.startswith("SELECT a.attname AS name"):
                return [
                    dict(
                        name="id",
                        sql_type="bigint",
                        nullable=False,
                        primary_key=True,
                        default_sql=None,
                    ),
                    dict(
                        name="parent_id",
                        sql_type="bigint",
                        nullable=False,
                        primary_key=False,
                        default_sql=None,
                    ),
                ]
            if sql.startswith("SELECT ci.relname AS index_name"):
                return []
            if "FROM pg_catalog.pg_constraint c" in sql:
                return self.fk_rows
        raise AssertionError(sql)


def fk_row(dialect, *, update="NO ACTION", delete="NO ACTION", count=1):
    row = dict(
        source_column="parent_id", target_table="parents", target_column="id", column_count=count
    )
    if dialect == "mysql":
        row.update(
            constraint_name="fk_child_parent",
            update_rule=update,
            delete_rule=delete,
            source_schema="aurorasql_test",
            target_schema="aurorasql_test",
        )
    else:
        codes = {
            "NO ACTION": "a",
            "RESTRICT": "r",
            "CASCADE": "c",
            "SET NULL": "n",
            "SET DEFAULT": "d",
        }
        row.update(
            constraint_name="fk_child_parent",
            update_code=codes[update],
            delete_code=codes[delete],
            same_schema=True,
            validated=True,
            deferrable=False,
            match_type="s",
        )
    return row


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_inspection_verifies_single_column_foreign_key(dialect):
    db = FakeDatabase(dialect, [fk_row(dialect)])
    result = inspect_table(db, "children")
    assert result.constraints_verified
    assert result.foreign_keys == (("parent_id", "parents", "id"),)
    assert result.foreign_key_actions == (("parent_id", "NO ACTION", "NO ACTION"),)
    assert all(params == {"name": "children"} for _, params in db.queries)
    plan = planning.plan_schema(db, Child)
    assert not any("foreign-key" in x.lower() for x in plan.warnings)
    assert not any(change.kind.startswith("foreign_key") for change in plan.changes)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize(
    "update,delete",
    [("CASCADE", "NO ACTION"), ("NO ACTION", "SET NULL"), ("RESTRICT", "SET DEFAULT")],
)
def test_server_action_drift_is_detected(dialect, update, delete):
    db = FakeDatabase(dialect, [fk_row(dialect, update=update, delete=delete)])
    result = planning.plan_schema(db, Child)
    assert "foreign_key_action_mismatch" in {change.kind for change in result.changes}
    assert result.requires_review
    assert not result.execution_allowed


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_missing_target_is_not_claimed_equivalent(dialect):
    result = planning.plan_schema(FakeDatabase(dialect, []), Child)
    assert "foreign_key_mismatch" in {change.kind for change in result.changes}


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_composite_foreign_key_is_not_verified(dialect):
    db = FakeDatabase(dialect, [fk_row(dialect, count=2)])
    inspected = inspect_table(db, "children")
    assert not inspected.constraints_verified
    assert inspected.foreign_key_actions is None
    result = planning.plan_schema(db, Child)
    assert result.requires_review
    assert any("not been verified" in item for item in result.warnings)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_cross_schema_fk_not_verified_as_same_database(dialect):
    foreign_key = fk_row(dialect)
    if dialect == "mysql":
        foreign_key["target_schema"] = "other_database"
    else:
        foreign_key["same_schema"] = False
    result = inspect_table(FakeDatabase(dialect, [foreign_key]), "children")
    assert not result.constraints_verified
    assert result.foreign_key_actions is None


@pytest.mark.parametrize(
    "flag,value", [("validated", False), ("deferrable", True), ("match_type", "f")]
)
def test_postgresql_unproven_constraint_semantics_require_review(flag, value):
    row = fk_row("postgresql")
    row[flag] = value
    result = inspect_table(FakeDatabase("postgresql", [row]), "children")
    assert not result.constraints_verified
    assert result.foreign_key_actions is None
