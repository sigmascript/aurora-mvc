"""DECIMAL precision/scale transitions must be based on stored values."""

from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import migration_review as review
from aurora.database.models import Model, column
from aurora.database.schema_planning import SchemaChange, SchemaPlan


@pytest.fixture
def decimal_catalog(monkeypatch):
    state = {"type": "DECIMAL(12,4)", "unsafe": False, "sql": []}

    def plan(connection, model):
        return SchemaPlan(
            model.__table_name__, True, (SchemaChange("type_mismatch", "amount", ""),), ()
        )

    def inspect(connection, name):
        primary = "PRIMARY" if connection.dialect == "mysql" else f"{name}_pkey"
        return SimpleNamespace(
            exists=True,
            uniqueness_verified=True,
            constraints_verified=True,
            indexes=(primary,),
            unique_columns=(),
            columns=(
                SimpleNamespace(
                    name="id", sql_type="BIGINT", nullable=False, primary_key=True, default_sql=None
                ),
                SimpleNamespace(
                    name="amount",
                    sql_type=state["type"],
                    nullable=True,
                    primary_key=False,
                    default_sql=None,
                ),
            ),
        )

    monkeypatch.setattr(review, "plan_schema", plan)
    monkeypatch.setattr(review, "inspect_table", inspect)
    return state


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize(
    "target", ["DECIMAL(10,2)", "DECIMAL(9,4)", "DECIMAL(12,1)", "DECIMAL(10,5)"]
)
def test_safe_decimal_reduction_is_reviewed(dialect, target, decimal_catalog):
    class Ledger(Model):
        __table__ = "ledger"
        id = column(int, primary_key=True)
        amount = column(target, not_null=False)

    def fetch(sql):
        decimal_catalog["sql"].append(sql)
        return None

    connection = SimpleNamespace(dialect=dialect, fetch_first=fetch)
    proposal = review.review_server_model_changes(connection, (Ledger,), "default", "identity")
    assert "ALTER TABLE " in proposal.statements[0]
    assert target in proposal.statements[0]
    assert len(decimal_catalog["sql"]) == 1
    statement = decimal_catalog["sql"][0]
    assert "ROUND(" in statement and "ABS(" in statement and "LIMIT 1" in statement
    assert proposal.operations[0]["edits"][0]["before"]["type"] == "decimal(12,4)"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_unsafe_decimal_reduction_refuses_before_ddl(dialect, decimal_catalog):
    class Ledger(Model):
        __table__ = "ledger"
        id = column(int, primary_key=True)
        amount = column("DECIMAL(10,2)", not_null=False)

    connection = SimpleNamespace(dialect=dialect, fetch_first=lambda sql: {"found": 1})
    with pytest.raises(ValueError, match="exceeds integral capacity or needs rounding"):
        review.review_server_model_changes(connection, (Ledger,), "default", "identity")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_decimal_reinspection_digest_tracks_catalog(dialect, decimal_catalog):
    class Ledger(Model):
        __table__ = "ledger"
        id = column(int, primary_key=True)
        amount = column("DECIMAL(10,2)", not_null=False)

    connection = SimpleNamespace(dialect=dialect, fetch_first=lambda sql: None)
    original = review.review_server_model_changes(connection, (Ledger,), "default", "identity")
    decimal_catalog["type"] = "DECIMAL(13,4)"
    different = review.review_server_model_changes(connection, (Ledger,), "default", "identity")
    assert original.review_sha256 != different.review_sha256


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_decimal_risk_rechecked_under_migration_lock(
    decimal_catalog, monkeypatch, tmp_path, dialect
):
    """A stale safe preview must never authorize an unsafe ALTER."""
    from contextlib import contextmanager

    from aurora.database import cli_init
    from aurora.database import migration_execution as executor

    class Ledger(Model):
        __table__ = "ledger"
        id = column(int, primary_key=True)
        amount = column("DECIMAL(10,2)", not_null=False)

    state = {"unsafe": False, "writes": 0}

    class Connection:
        _transaction_active = False
        _migration_lock_active = False

        def __init__(self):
            self.dialect = dialect

        def fetch_first(self, sql, *args):
            if "pg_try_advisory_xact_lock" in sql:
                return {"acquired": True}
            return {"found": 1} if state["unsafe"] else None

        def execute_write(self, *args):
            state["writes"] += 1

        @contextmanager
        def transaction(self):
            yield

    @contextmanager
    def lock(conn):
        yield

    connection = Connection()
    proposal = review.review_server_model_changes(connection, (Ledger,), "default", "identity")
    state["unsafe"] = True
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(executor, "read_history", lambda *_: ([{"id": 1}], {}))
    monkeypatch.setattr(executor, "history_table", lambda *_: "__migrations__")
    monkeypatch.setattr(executor, "migration_lock", lock)
    with pytest.raises(ValueError, match="needs rounding"):
        executor.execute_server_forward_migration(
            connection,
            (Ledger,),
            tmp_path,
            "default",
            identity="identity",
            parsed=object(),
            review=proposal,
            approval=proposal.review_sha256,
            title="Decimal reduction",
            backup_path=None,
            config_path="dummy",
        )
    assert state["writes"] == 0
