"""Nullability review is read-only and refuses mixed or unsafe edits."""

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database.planning import SchemaChange, SchemaPlan


class RequiredLabel(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    label = column(str, length=80)


class OptionalLabel(Model):
    __table__ = "labels"
    id = column(int, primary_key=True)
    label = column(str, length=80, nullable=True)


class FakeConnection:
    def __init__(self, dialect, has_nulls=False):
        self.dialect = dialect
        self.has_nulls = has_nulls
        self.reads = []

    def fetch_first(self, statement, params=None):
        self.reads.append(statement)
        return {"found": 1} if self.has_nulls else None


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize("model,previous_nullable", [(RequiredLabel, True),
                                                       (OptionalLabel, False)])
def test_nullability_review_is_read_only_and_dialect_specific(monkeypatch, dialect,
                                                                model, previous_nullable):
    from aurora.database import server_nullability_review

    index = "PRIMARY" if dialect == "mysql" else "labels_pkey"
    table = TableInfo("labels", True,
                      (ColumnInfo("id", "BIGINT", False, True),
                       ColumnInfo("label", "VARCHAR(80)", previous_nullable, False)),
                      indexes=(index,), uniqueness_verified=True)
    monkeypatch.setattr(server_nullability_review, "inspect_table", lambda *_: table)
    monkeypatch.setattr(server_nullability_review, "plan_schema", lambda *args:
                        SchemaPlan("labels", True,
                                   (SchemaChange("nullability_mismatch", "label", "differs"),), ()))
    connection = FakeConnection(dialect)
    proposal = server_nullability_review.review_server_nullability_change(
        connection, [model], "accounting", "accounting-id")

    assert proposal.execution_allowed is False
    assert proposal.operations[0]["from"] is previous_nullable
    assert proposal.operations[0]["to"] is model.__columns__["label"].nullable
    assert len(proposal.statements) == 1
    if model is RequiredLabel:
        assert "NOT NULL" in proposal.statements[0]
    elif dialect == "postgresql":
        assert "DROP NOT NULL" in proposal.statements[0]
    else:
        assert proposal.statements[0].endswith(" NULL")
    assert len(connection.reads) == (1 if model is RequiredLabel else 0)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_nullability_reviewer_refuses_existing_nulls_and_unverified_indexes(monkeypatch, dialect):
    from aurora.database import server_nullability_review

    table = TableInfo("labels", True,
                      (ColumnInfo("id", "BIGINT", False, True),
                       ColumnInfo("label", "VARCHAR(80)", True, False)),
                      indexes=("PRIMARY" if dialect == "mysql" else "labels_pkey",),
                      uniqueness_verified=True)
    monkeypatch.setattr(server_nullability_review, "inspect_table", lambda *_: table)
    monkeypatch.setattr(server_nullability_review, "plan_schema", lambda *args:
                        SchemaPlan("labels", True,
                                   (SchemaChange("nullability_mismatch", "label", "differs"),), ()))
    connection = FakeConnection(dialect, has_nulls=True)

    with pytest.raises(ValueError, match="Existing NULL"):
        server_nullability_review.review_server_nullability_change(
            connection, [RequiredLabel], "accounting", "accounting-id")

    extra_index = TableInfo("labels", True, table.columns,
                            indexes=(*table.indexes, "unverified_index"),
                            uniqueness_verified=True)
    monkeypatch.setattr(server_nullability_review, "inspect_table", lambda *_: extra_index)

    with pytest.raises(ValueError, match="indexed"):
        server_nullability_review.review_server_nullability_change(
            FakeConnection(dialect), [RequiredLabel], "accounting", "accounting-id")
