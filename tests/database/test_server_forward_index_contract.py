"""Lean safety contracts for server ordinary-index forward successors."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import constraints as subject


class CatalogConnection:
    def __init__(self, dialect, *, present=False, fk=False, complex=False):
        self.dialect = dialect
        self.present = present
        self.fk = fk
        self.complex = complex
        self.calls = []

    def fetch_all(self, sql, params=None):
        self.calls.append((sql, params))
        if self.dialect == "mysql":
            if "information_schema.STATISTICS" in sql:
                if not self.present:
                    return []
                return [
                    dict(
                        index_name="notes_title_idx",
                        column_name="title",
                        non_unique=0 if self.complex else 1,
                        position=1,
                        method="BTREE",
                        prefix_length=None,
                        direction="A",
                    )
                ]
            if "information_schema.KEY_COLUMN_USAGE" in sql:
                return [{"column_name": "title"}] if self.fk else []
        else:
            if "pg_catalog.pg_index" in sql and "generate_series" not in sql:
                if not self.present:
                    return []
                return [
                    dict(
                        is_unique=self.complex,
                        is_primary=False,
                        is_valid=True,
                        is_ready=True,
                        is_partial=False,
                        is_expression=False,
                        key_count=1,
                        total_count=1,
                        method="btree",
                        constraint_backed=False,
                    )
                ]
            if "generate_series" in sql:
                return [dict(column_name="title", definition="title")]
            if "pg_catalog.pg_constraint" in sql:
                return [{"column_name": "title"}] if self.fk else []
        raise AssertionError("Unexpected catalog query: " + sql)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_index_catalog_identifies_single_ordinary_column(dialect):
    db = CatalogConnection(dialect, present=True)
    assert subject._index_catalog(db, "notes", "notes_title_idx") == ("title",)
    assert all(
        call[1] == {"table": "notes", "name": "notes_title_idx"} or call[1] == {"table": "notes"}
        for call in db.calls
    )


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_absent_index_is_not_assumed_present(dialect):
    db = CatalogConnection(dialect)
    assert subject._index_catalog(db, "notes", "notes_title_idx") is None


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_unique_index_is_not_droppable_via_ordinary_path(dialect):
    db = CatalogConnection(dialect, present=True, complex=True)
    with pytest.raises(ValueError, match="ordinary B-tree"):
        subject._index_catalog(db, "notes", "notes_title_idx", drop_safety=True)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_fk_supporting_index_is_not_droppable_but_can_be_observed(dialect):
    db = CatalogConnection(dialect, present=True, fk=True)
    assert subject._index_catalog(db, "notes", "notes_title_idx") == ("title",)
    with pytest.raises(ValueError, match="foreign-key"):
        subject._index_catalog(db, "notes", "notes_title_idx", drop_safety=True)


class FailingConnection:
    def __init__(self, dialect):
        self.dialect = dialect
        self.events = []

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        except BaseException:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    def fetch_first(self, query, params):
        self.events.append("lock")
        return {"acquired": True}

    def execute_write(self, statement, *args):
        self.events.append("ddl")
        raise RuntimeError("simulated driver DDL failure")


@pytest.fixture
def execution_stubs(monkeypatch):
    from aurora.database import cli_init

    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(
        subject,
        "_review",
        lambda *_: (
            {"statement": "CREATE INDEX notes_title_idx ON notes (title)", "columns": ("title",)},
            "verified",
            1,
        ),
    )

    @contextmanager
    def fake_mysql_lock(connection):
        connection.events.append("lock")
        yield

    monkeypatch.setattr(subject, "migration_lock", fake_mysql_lock)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_stale_review_never_executes_index_ddl(dialect, execution_stubs):
    connection = FailingConnection(dialect)
    with pytest.raises(RuntimeError, match="proposal changed"):
        subject._apply(
            connection, (), ".", "default", "identity", object(), {"kind": "create_index"}, "stale"
        )
    assert "ddl" not in connection.events


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_failed_ddl_preserves_backend_failure_boundary(dialect, execution_stubs):
    connection = FailingConnection(dialect)
    expected = "outcome uncertain" if dialect == "mysql" else "simulated driver DDL failure"
    with pytest.raises(RuntimeError, match=expected):
        subject._apply(
            connection,
            (),
            ".",
            "default",
            "identity",
            object(),
            {"kind": "create_index"},
            "verified",
        )
    assert connection.events.count("ddl") == 1
    if dialect == "postgresql":
        assert connection.events == ["begin", "lock", "ddl", "rollback"]


def test_mysql_and_postgresql_ddl_syntax():
    assert subject._quote("notes", "mysql") == "`notes`"
    assert subject._quote("notes", "postgresql") == '"notes"'


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_preflight_trusts_only_catalog_verified_history_indexes(monkeypatch, dialect):
    from aurora.database import cli_init, migration_history, snapshots

    model = SimpleNamespace(__table_name__="notes")

    class FakeConnection:
        def __init__(self):
            self.dialect = dialect

        def fetch_all(self, *_):
            return (
                [{"name": "notes"}, {"name": "__migrations__"}]
                if dialect == "mysql"
                else [{"name": "notes"}, {"name": "__migrations__"}]
            )

    seen = []

    def plan(_connection, _model, *, models, trusted_indexes=()):
        seen.append(tuple(trusted_indexes))
        return SimpleNamespace(
            table="notes",
            exists=True,
            changes=(),
            warnings=() if "notes_title_idx" in trusted_indexes else ("Index not yet trusted",),
        )

    monkeypatch.setattr(cli_init, "plan_schema", plan)
    monkeypatch.setattr(
        migration_history,
        "read_history",
        lambda *_: (
            [{"id": 1}],
            {
                "models": ["validated-model"],
                "physical_schema": [{"table": "notes", "indexes": ["notes_title_idx"]}],
            },
        ),
    )
    monkeypatch.setattr(
        snapshots, "schema_snapshot", lambda *_args, **_kw: {"tables": ["validated-model"]}
    )
    result = cli_init._preflight(FakeConnection(), [model], "default")
    assert result["status"] == "ALREADY_INITIALIZED"
    assert seen == [(), ("notes_title_idx",)]


def test_server_index_title_passes_through_approval(monkeypatch, tmp_path, capsys):
    from contextlib import contextmanager

    from aurora.database import cli_check, cli_init

    class DB:
        dialect = "mysql"

    class Config:
        def resolve(self, alias):
            return "mysql+pymysql://example@localhost:3306/test"

        @contextmanager
        def connect(self, alias):
            yield DB()

    monkeypatch.setattr(cli_check, "_select_models", lambda *_: (["Notes"], ["model"]))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(
        subject, "_review", lambda *_: ({"statement": "CREATE INDEX ..."}, "digest", 1)
    )
    observed = []
    monkeypatch.setattr(
        subject, "_apply", lambda *args: observed.append(args[-1]) or "snapshot.json"
    )
    args = SimpleNamespace(
        database="default",
        title="Index notes title",
        apply=False,
        approve=None,
        backup=None,
        dry_run=False,
        format="text",
    )
    operation = {"kind": "create_index", "table": "notes", "name": "notes_title_idx"}
    assert (
        subject.run_server_index_operation(
            args,
            {"operation": operation},
            Config(),
            tmp_path,
            simple_confirm=True,
            interactive=False,
        )
        == 0
    )
    assert observed == ["Index notes title"]


def test_server_index_rejects_invalid_title_before_connection(tmp_path):
    class Config:
        def resolve(self, alias):
            raise AssertionError("Must not connect before validating title")

    args = SimpleNamespace(
        database="default",
        title="bad\nname",
        apply=False,
        approve=None,
        backup=None,
        dry_run=False,
        format="text",
    )
    with pytest.raises(ValueError, match="control characters"):
        subject.run_server_index_operation(
            args,
            {"operation": {"kind": "create_index"}},
            Config(),
            tmp_path,
            simple_confirm=True,
            interactive=False,
        )
