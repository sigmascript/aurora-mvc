"""Batch B: read-only, fail-closed migration preflight contracts."""

from dataclasses import replace

import pytest

from aurora.aurorasql.migration_format import migration_plan
from aurora.aurorasql.migration_preflight import preflight_migration
from aurora.aurorasql.snapshots import schema_snapshot
from aurora.aurorasql.models import Column, Model


class Original(Model):
    __table_name__ = "preflight_records"
    id = Column(int, primary_key=True)


class Extended(Model):
    __table_name__ = "preflight_records"
    id = Column(int, primary_key=True)
    note = Column(str, nullable=True)


class FakeConnection:
    dialect = "sqlite"

    def __init__(self, exists=True, columns=("id",)):
        self.exists = exists
        self.columns = columns
        self.queries = []

    def fetch_first(self, sql, params=None):
        self.queries.append(sql)
        return {"name": "preflight_records"} if self.exists else None

    def fetch_all(self, sql, params=None):
        self.queries.append(sql)
        if sql.startswith("PRAGMA table_info"):
            return [{"name": name, "type": "INTEGER", "notnull": 0,
                     "pk": int(name == "id"), "dflt_value": None} for name in self.columns]
        return []


def artifacts():
    before = schema_snapshot(Original, dialect="sqlite")
    after = schema_snapshot(Extended, dialect="sqlite")
    return before, after, migration_plan(before, after, migration_id="a7_add_note")


def test_preflight_is_read_only_and_never_authorizes_execution():
    before, after, plan = artifacts()
    connection = FakeConnection()
    result = preflight_migration(connection, before, after, plan)
    assert result.ready_for_execution is False
    assert result.migration_id == "a7_add_note"
    assert all(sql.startswith(("SELECT", "PRAGMA")) for sql in connection.queries)
    assert any("no migration execution" in item for item in result.findings)


@pytest.mark.parametrize("columns,exists", [(("id", "unexpected"), True), (("id",), False)])
def test_preflight_detects_live_drift(columns, exists):
    before, after, plan = artifacts()
    result = preflight_migration(FakeConnection(exists, columns), before, after, plan)
    assert any("expected" in finding or "differ" in finding for finding in result.findings)


def test_preflight_rejects_modified_snapshot():
    before, after, plan = artifacts()
    after["tables"][0]["columns"][1]["nullable"] = False
    with pytest.raises(ValueError, match="fingerprint"):
        preflight_migration(FakeConnection(), before, after, plan)


def test_preflight_rejects_forged_operation():
    before, after, plan = artifacts()
    plan["operations"][0]["risk"] = "potential_data_loss"
    with pytest.raises(ValueError, match="operations"):
        preflight_migration(FakeConnection(), before, after, plan)


def test_preflight_rejects_dialect_mismatch():
    before, after, plan = artifacts()
    connection = FakeConnection()
    connection.dialect = "mysql"
    with pytest.raises(ValueError, match="dialect"):
        preflight_migration(connection, before, after, plan)
