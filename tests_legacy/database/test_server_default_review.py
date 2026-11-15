"""Default additions are single, literal, read-only proposals."""

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database.planning import SchemaChange, SchemaPlan


class DefaultedLabel(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    label = column(str, length=80, default="new")


class NoDefaultLabel(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    label = column(str, length=80)


class Connection:
    def __init__(self, dialect):
        self.dialect = dialect


@pytest.mark.parametrize("dialect,quoted", [("mysql", "`"), ("postgresql", '"')])
def test_default_addition_proposes_only_validated_literal(monkeypatch, dialect, quoted):
    from aurora.database import server_default_review

    table = TableInfo("labels", True,
                      (ColumnInfo("id", "BIGINT", False, True),
                       ColumnInfo("label", "VARCHAR(80)", False, False)),
                      indexes=("PRIMARY" if dialect == "mysql" else "labels_pkey",),
                      uniqueness_verified=True)
    monkeypatch.setattr(server_default_review, "inspect_table", lambda *_: table)
    monkeypatch.setattr(server_default_review, "plan_schema", lambda *_:
                        SchemaPlan("labels", True,
                                   (SchemaChange("default_mismatch", "label", "differs"),), ()))
    proposal = server_default_review.review_server_default_addition(
        Connection(dialect), [DefaultedLabel], "accounting", "accounting-id")

    assert proposal.operations == ({"kind": "add_default", "table": "labels",
                                    "column": "label"},)
    assert proposal.statements[0] == (f"ALTER TABLE {quoted}labels{quoted} "
                                      f"ALTER COLUMN {quoted}label{quoted} SET DEFAULT 'new'")
    assert proposal.execution_allowed is False

    with pytest.raises(ValueError, match="manual review"):
        server_default_review.review_server_default_addition(
            Connection(dialect), [NoDefaultLabel], "accounting", "accounting-id")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_default_review_refuses_replacing_existing_default(monkeypatch, dialect):
    from aurora.database import server_default_review

    table = TableInfo("labels", True,
                      (ColumnInfo("id", "BIGINT", False, True),
                       ColumnInfo("label", "VARCHAR(80)", False, False, "old")),
                      indexes=("PRIMARY" if dialect == "mysql" else "labels_pkey",),
                      uniqueness_verified=True)
    monkeypatch.setattr(server_default_review, "inspect_table", lambda *_: table)
    monkeypatch.setattr(server_default_review, "plan_schema", lambda *_:
                        SchemaPlan("labels", True,
                                   (SchemaChange("default_mismatch", "label", "differs"),), ()))

    with pytest.raises(ValueError, match="manual review"):
        server_default_review.review_server_default_addition(
            Connection(dialect), [DefaultedLabel], "accounting", "accounting-id")
