"""Create-only server FK initialization must review exact models and schema."""

from types import SimpleNamespace

import pytest

from aurora.database import initialization, schema_inspection
from aurora.database.cli_init import _mysql_approval_digest, _postgres_approval_digest
from aurora.database.models import Model, column
from aurora.database.schema import create_table_sql, validate_init_models
from aurora.database.schema_inspection import ColumnInfo, TableInfo
from aurora.database.snapshots import schema_snapshot


class Parent(Model):
    __table__ = "fk_parent"
    id = column(int, primary_key=True)


class Child(Model):
    __table__ = "fk_child"
    id = column(int, primary_key=True)
    parent_id = column(int, reference="Parent.id", on_delete="CASCADE")


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_fk_ddl_approval_and_snapshot_use_selected_models(dialect):
    models = validate_init_models((Child, Parent), dialect=dialect, allow_extended=True)
    assert models == (Parent, Child)
    sql = create_table_sql(Child, dialect, models=models)
    assert "FOREIGN KEY" in sql and "CASCADE" in sql
    digest = _mysql_approval_digest if dialect == "mysql" else _postgres_approval_digest
    approved = digest(models, "default", "verified-identity")
    assert approved != digest((Parent,), "default", "verified-identity")
    snapshot = schema_snapshot(*models, dialect=dialect, allow_v2=True)
    assert snapshot["format"] == "aurorasql.schema.v2"
    assert snapshot["tables"][0]["name"] == "fk_child"


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_server_snapshot_records_verified_fk_evidence(tmp_path, monkeypatch, dialect):
    observed = [
        {"table": "fk_child", "foreign_keys": [["parent_id", "fk_parent", "id"]]},
        {"table": "fk_parent", "foreign_keys": []},
    ]
    monkeypatch.setattr(initialization, "_physical", lambda _: observed)
    path = initialization._write_snapshot(
        SimpleNamespace(dialect=dialect), tmp_path, "default", (Parent, Child), "Initial"
    )
    import json

    payload = json.loads(path.read_text())
    assert payload["physical_schema"] == observed
    assert next(item for item in payload["models"] if item["name"] == "fk_child")


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_fk_catalog_mismatch_is_refused_before_recording(monkeypatch, dialect):
    table = TableInfo(
        "fk_child",
        True,
        (ColumnInfo("id", "BIGINT", False, True), ColumnInfo("parent_id", "BIGINT", False, False)),
        (),
        ("PRIMARY",) if dialect == "mysql" else ("fk_child_pkey",),
        True,
        True,
        (),
        (),
    )
    monkeypatch.setattr(schema_inspection, "inspect_table", lambda *_: table)
    with pytest.raises(RuntimeError, match="foreign-key target"):
        schema_inspection._verify_declared_table(
            SimpleNamespace(dialect=dialect), Child, (Parent, Child)
        )


def test_mysql_no_action_and_restrict_are_equivalent_in_read_only_check(monkeypatch):
    from aurora.database import schema_planning as planning

    table = TableInfo(
        "fk_child",
        True,
        (ColumnInfo("id", "BIGINT", False, True), ColumnInfo("parent_id", "BIGINT", False, False)),
        (),
        ("PRIMARY", "parent_id"),
        True,
        True,
        (("parent_id", "fk_parent", "id"),),
        (("parent_id", "RESTRICT", "CASCADE"),),
    )
    monkeypatch.setattr(planning, "inspect_table", lambda *_: table)
    report = planning.plan_schema(SimpleNamespace(dialect="mysql"), Child, models=(Parent, Child))
    assert not report.changes


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_expected_server_fk_catalog_is_accepted(monkeypatch, dialect):
    table = TableInfo(
        "fk_child",
        True,
        (ColumnInfo("id", "BIGINT", False, True), ColumnInfo("parent_id", "BIGINT", False, False)),
        (),
        ("PRIMARY", "parent_id") if dialect == "mysql" else ("fk_child_pkey",),
        True,
        True,
        (("parent_id", "fk_parent", "id"),),
        (("parent_id", "RESTRICT" if dialect == "mysql" else "NO ACTION", "CASCADE"),),
    )
    monkeypatch.setattr(schema_inspection, "inspect_table", lambda *_: table)

    class Catalog:
        def __init__(self):
            self.dialect = dialect

        def fetch_all(self, sql, params=None):
            if "information_schema.STATISTICS" in sql:
                return [
                    {"name": "PRIMARY", "col": "id", "non_unique": 0, "position": 1},
                    {"name": "parent_id", "col": "parent_id", "non_unique": 1, "position": 1},
                ]
            if "information_schema.TABLE_CONSTRAINTS" in sql:
                return [{"kind": "PRIMARY KEY"}, {"kind": "FOREIGN KEY"}]
            if "information_schema.COLUMNS" in sql:
                return [
                    {"name": "id", "extra": "auto_increment", "generation": ""},
                    {"name": "parent_id", "extra": "", "generation": ""},
                ]
            if "pg_catalog.pg_constraint" in sql:
                return [{"kind": "p"}, {"kind": "f"}]
            if "pg_catalog.pg_attribute" in sql:
                return [
                    {"name": "id", "identity": "d", "generated": ""},
                    {"name": "parent_id", "identity": "", "generated": ""},
                ]
            raise AssertionError("Unexpected catalog query")

        def fetch_first(self, sql, params=None):
            if "information_schema.TRIGGERS" in sql:
                return None
            if "pg_catalog.pg_class" in sql:
                return {
                    "persistence": "p",
                    "partitioned": False,
                    "inherited": False,
                    "rls": False,
                    "triggers": 0,
                    "parents": 0,
                }
            raise AssertionError("Unexpected catalog query")

    assert schema_inspection._verify_declared_table(Catalog(), Child, (Parent, Child)) == table
