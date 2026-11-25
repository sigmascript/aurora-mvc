"""Backend-independent acceptance of one-statement server additive transitions."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import migration_execution as executor
from aurora.database import migration_review as reviewer
from aurora.database.models import Model, column
from aurora.database.schema_planning import SchemaChange, SchemaPlan


class Notes(Model):
    __table__ = "phase1_notes"
    id = column(int, primary_key=True)
    caption = column(str, length=80, not_null=False)
    visits = column(int, default=7)
    comment = column(str, length=40, default="it's fine")


@pytest.fixture
def catalog(monkeypatch):
    state = {"changes": ("caption", "visits", "comment"), "warnings": ()}

    def fake_plan(connection, model):
        return SchemaPlan(
            model.__table_name__,
            True,
            tuple(SchemaChange("missing_column", name, "absent") for name in state["changes"]),
            state["warnings"],
        )

    monkeypatch.setattr(reviewer, "plan_schema", fake_plan)
    monkeypatch.setattr(
        reviewer,
        "inspect_table",
        lambda *args: SimpleNamespace(
            uniqueness_verified=True,
            indexes=("PRIMARY",) if args[0].dialect == "mysql" else ("phase1_notes_pkey",),
        ),
    )
    return state


@pytest.mark.parametrize("dialect,quote", [("mysql", "`"), ("postgresql", '"')])
def test_multiple_additions_use_one_reviewed_server_statement(catalog, dialect, quote):
    connection = SimpleNamespace(dialect=dialect)
    result = reviewer.review_server_model_changes(connection, (Notes,), "default", "verified")
    assert result.operations == (
        {
            "kind": "add_columns",
            "table": "phase1_notes",
            "columns": ("caption", "visits", "comment"),
        },
    )
    assert len(result.statements) == 1
    sql = result.statements[0]
    assert sql.startswith(f"ALTER TABLE {quote}phase1_notes{quote} ")
    assert sql.count("ADD COLUMN") == 3
    assert "DEFAULT 7 NOT NULL" in sql
    assert "DEFAULT 'it''s fine' NOT NULL" in sql
    assert "caption" in sql
    assert (
        result.review_sha256
        == reviewer.review_server_model_changes(
            connection, (Notes,), "default", "verified"
        ).review_sha256
    )
    catalog["changes"] = ("caption",)
    assert (
        result.review_sha256
        != reviewer.review_server_model_changes(
            connection, (Notes,), "default", "verified"
        ).review_sha256
    )


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_mixed_existing_column_drift_cannot_be_hidden(catalog, monkeypatch, dialect):
    monkeypatch.setattr(
        reviewer,
        "plan_schema",
        lambda c, m: SchemaPlan(
            m.__table_name__,
            True,
            (
                SchemaChange("missing_column", "caption", "absent"),
                SchemaChange("type_mismatch", "visits", "drift"),
            ),
            (),
        ),
    )
    with pytest.raises(ValueError, match="unsupported existing-column drift"):
        reviewer.review_server_model_changes(
            SimpleNamespace(dialect=dialect), (Notes,), "default", "verified"
        )


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_unknown_catalog_warning_prevents_addition(catalog, dialect):
    catalog["warnings"] = ("Unverified constraint",)
    with pytest.raises(ValueError, match="verified physical schema"):
        reviewer.review_server_model_changes(
            SimpleNamespace(dialect=dialect), (Notes,), "default", "verified"
        )


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_missing_default_for_new_required_field_is_refused(dialect):
    class Unsafe(Model):
        __table__ = "unsafe"
        id = column(int, primary_key=True)
        required = column(str, length=10)

    with pytest.raises(ValueError, match="nullable field or a validated literal default"):
        reviewer._add_column_statement(Unsafe, "required", dialect)


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_multi_addition_exact_review_is_rechecked_under_lock(monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init

    operation = {"kind": "add_columns", "table": "phase1_notes", "columns": ("caption", "visits")}
    review = SimpleNamespace(
        operations=(operation,),
        statements=("ALTER TABLE phase1_notes ADD COLUMN caption TEXT, ADD COLUMN visits INT",),
        review_sha256="approved",
    )
    events = []

    class Connection:
        _transaction_active = False
        _migration_lock_active = False

        def __init__(self):
            self.dialect = dialect

        @contextmanager
        def transaction(self):
            events.append("begin")
            try:
                yield
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

        def fetch_first(self, *args):
            events.append("advisory-lock")
            return {"acquired": True}

        def execute_write(self, sql, *args):
            events.append("ddl")
            raise RuntimeError("simulated failure")

    @contextmanager
    def mysql_lock(connection):
        events.append("mysql-lock")
        yield

    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(executor, "read_history", lambda *_: ([{"id": 1}], {}))
    monkeypatch.setattr(executor, "history_table", lambda *_: "__migrations__")
    monkeypatch.setattr(executor, "review_server_model_changes", lambda *_: review)
    monkeypatch.setattr(executor, "migration_lock", mysql_lock)

    with pytest.raises(
        RuntimeError, match="outcome uncertain" if dialect == "mysql" else "simulated failure"
    ):
        executor.execute_server_forward_migration(
            Connection(),
            (Notes,),
            tmp_path,
            "default",
            identity="identity",
            parsed=object(),
            review=review,
            approval="approved",
            title="Add fields",
            backup_path=None,
            config_path="dummy",
        )
    assert events.count("ddl") == 1
    assert events.index("ddl") > 0
    if dialect == "postgresql":
        assert events[-1] == "rollback"


def test_mysql_text_literal_default_is_not_assumed_portable():
    class Text(Model):
        __table__ = "notes_text"
        id = column(int, primary_key=True)
        note = column(str, default="example")

    with pytest.raises(ValueError, match="TEXT/BLOB defaults"):
        reviewer._add_column_statement(Text, "note", "mysql")
    assert "DEFAULT 'example'" in reviewer._add_column_statement(Text, "note", "postgresql")
