"""Cross-engine one-table destructive contract, no compulsory backups."""

from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import table_operations as removal
from aurora.database.schema_inspection import ColumnInfo, TableInfo


def schemas():
    old = [
        {
            "name": "items",
            "columns": [{"name": "id", "primary_key": True}],
            "create_sql_preview": "old",
        },
        {
            "name": "child",
            "columns": [{"name": "id", "primary_key": True}],
            "create_sql_preview": "old",
        },
    ]
    new = deepcopy(old[:1])
    new[0]["create_sql_preview"] = "new"
    return old, new


def test_only_one_removed_table_without_unrelated_edit():
    old, new = schemas()
    assert removal.single_table_removal(old, new)
    assert not removal.single_table_removal(old, [])
    assert not removal.single_table_removal(old, [{"name": "other", "columns": []}])
    assert not removal.single_table_removal(old, [old[0], {"name": "child", "columns": []}])
    assert not removal.single_table_removal(["changed"], new)
    assert not removal.single_table_removal(old, new + [{"name": "other", "columns": []}])


class Fake:
    def __init__(self, dialect, rows):
        self.dialect, self.rows = dialect, rows
        self.statements = []

    def fetch_first(self, sql, params=None):
        if "COUNT(*) AS total" in sql:
            return {"total": self.rows}
        if "pg_try_advisory_xact_lock" in sql:
            return {"acquired": True}
        return None

    def execute_write(self, sql, params=None):
        self.statements.append((sql, params))

    @contextmanager
    def transaction(self):
        yield


@pytest.fixture(params=["mysql", "postgresql"])
def dialect(request):
    return request.param


def setup(monkeypatch, dialect, rows=2, dependencies=False):
    connection = Fake(dialect, rows)
    old, new = schemas()
    models = [SimpleNamespace(__table_name__="items")]
    monkeypatch.setattr(removal, "validate_init_models", lambda *a, **k: models)
    monkeypatch.setattr(
        removal,
        "read_history",
        lambda *a: ([{"id": 1}], {"models": old, "physical_schema": [{"table": "child"}]}),
    )
    monkeypatch.setattr(removal, "schema_snapshot", lambda *a, **k: {"tables": new})
    expected_index = "PRIMARY" if dialect == "mysql" else "child_pkey"
    monkeypatch.setattr(
        removal,
        "inspect_table",
        lambda *_: TableInfo(
            "child",
            True,
            (ColumnInfo("id", "bigint", False, True),),
            (),
            (expected_index,),
            True,
            True,
            (),
            (),
        ),
    )
    monkeypatch.setattr(removal, "_incoming_foreign_key_dependency", lambda *a: dependencies)
    monkeypatch.setattr(removal, "_verify_removal_constraints", lambda *a, **k: None)
    return connection, models


def test_review_recognizes_populated_and_empty_tables(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    operation, digest, count = removal.review_table_removal(
        connection, models, ".", "data", "identity"
    )
    assert operation == {"kind": "drop_table", "table": "child", "rows": 2}
    assert count == 1 and len(digest) == 64
    connection.rows = 0
    assert removal.review_table_removal(connection, models, ".", "data", "identity")[0]["rows"] == 0


def test_review_refuses_fk_dependents(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect, dependencies=True)
    with pytest.raises(ValueError, match="Foreign-key"):
        removal.review_table_removal(connection, models, ".", "data", "identity")


def test_review_refuses_other_indexes(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    current = removal.inspect_table(connection, "child")
    monkeypatch.setattr(
        removal,
        "inspect_table",
        lambda *_: TableInfo(
            "child", True, current.columns, (), current.indexes + ("extra",), True, True, (), ()
        ),
    )
    with pytest.raises(ValueError, match="verified ordinary table"):
        removal.review_table_removal(connection, models, ".", "data", "identity")


def execution(monkeypatch, dialect, rows=2):
    connection, models = setup(monkeypatch, dialect, rows)
    operation = {"kind": "drop_table", "table": "child", "rows": rows}
    monkeypatch.setattr(removal, "review_table_removal", lambda *a: (operation, "a" * 64, 2))
    monkeypatch.setattr(removal.sys.stdin, "isatty", lambda: True)
    from aurora.database import cli_init

    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *a: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *a: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *a: "identity")
    monkeypatch.setattr(_identity_module, "_postgres_database_identity", lambda *a: "identity")
    monkeypatch.setattr(removal, "inspect_table", lambda *a: TableInfo("child", False, ()))
    monkeypatch.setattr(removal, "verify_server_declared_schema", lambda *a, **k: None)
    monkeypatch.setattr(removal, "_write_snapshot", lambda *a: SimpleNamespace(name="success.json"))
    monkeypatch.setattr(removal, "history_table", lambda *a: "__migrations__")
    monkeypatch.setattr(
        removal,
        "read_history",
        lambda *a: (
            [
                None,
                None,
                next(
                    params for sql, params in connection.statements if sql.startswith("INSERT INTO")
                ),
            ],
            {},
        ),
    )

    @contextmanager
    def lock(conn):
        yield

    monkeypatch.setattr(removal, "migration_lock", lock)
    args = SimpleNamespace(
        title="Remove child", confirm=None, apply=False, approve=None, backup=None, name=None
    )
    return connection, models, args


def test_populated_drop_without_backup_after_exact_consent(monkeypatch, dialect, capsys):
    connection, models, args = execution(monkeypatch, dialect)
    answers = iter(["y", "DELETE TABLE data.child"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert removal.run_table_removal(args, connection, models, ".", "data", "identity", None) == 0
    output = capsys.readouterr().out
    assert "WARNING" in output and "aurora db backup" in output
    assert sum("DROP TABLE" in sql for sql, _ in connection.statements) == 1
    record = next(params for sql, params in connection.statements if sql.startswith("INSERT INTO"))
    assert "Confirmed permanent data loss" in record["warnings"]


def test_cancellation_does_not_drop(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect)
    answers = iter(["y", "wrong acknowledgement"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    assert removal.run_table_removal(args, connection, models, ".", "data", "identity", None) == 0
    assert not any("DROP TABLE" in sql for sql, _ in connection.statements)


def test_empty_table_needs_only_ordinary_approval(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect, rows=0)
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert removal.run_table_removal(args, connection, models, ".", "data", "identity", None) == 0
    assert sum("DROP TABLE" in sql for sql, _ in connection.statements) == 1


def test_noninteractive_refusal(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect)
    monkeypatch.setattr(removal.sys.stdin, "isatty", lambda: False)
    with pytest.raises(PermissionError, match="interactive"):
        removal.run_table_removal(args, connection, models, ".", "data", "identity", None)


def test_mysql_history_failure_is_reported_as_uncertain(monkeypatch):
    connection, models, args = execution(monkeypatch, "mysql")
    answers = iter(["y", "DELETE TABLE data.child"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr(
        removal, "_write_snapshot", lambda *a: (_ for _ in ()).throw(OSError("snapshot failed"))
    )
    with pytest.raises(RuntimeError, match="outcome uncertain"):
        removal.run_table_removal(args, connection, models, ".", "data", "identity", None)
    assert any("DROP TABLE" in sql for sql, _ in connection.statements)


def test_postgres_history_failure_does_not_claim_success(monkeypatch):
    connection, models, args = execution(monkeypatch, "postgresql")
    answers = iter(["y", "DELETE TABLE data.child"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr(
        removal, "_write_snapshot", lambda *a: (_ for _ in ()).throw(OSError("snapshot failed"))
    )
    with pytest.raises(OSError, match="snapshot failed"):
        removal.run_table_removal(args, connection, models, ".", "data", "identity", None)
    assert any("DROP TABLE" in sql for sql, _ in connection.statements)


def test_changed_review_refuses_ddl(monkeypatch, dialect):
    connection, models, args = execution(monkeypatch, dialect)
    attempts = [0]

    def changing(*_):
        attempts[0] += 1
        return (
            {"kind": "drop_table", "table": "child", "rows": 2},
            ("a" * 64 if attempts[0] == 1 else "b" * 64),
            2,
        )

    monkeypatch.setattr(removal, "review_table_removal", changing)
    answers = iter(["y", "DELETE TABLE data.child"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    with pytest.raises(RuntimeError, match="proposal changed"):
        removal.run_table_removal(args, connection, models, ".", "data", "identity", None)
    assert not any("DROP TABLE" in sql for sql, _ in connection.statements)


def test_mysql_final_preflight_failure_is_not_called_uncertain(monkeypatch):
    connection, models, args = execution(monkeypatch, "mysql")
    answers = iter(["y", "DELETE TABLE data.child"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    calls = [0]

    def review(*_):
        calls[0] += 1
        if calls[0] == 3:
            raise ValueError("Physical evidence changed before DDL")
        return {"kind": "drop_table", "table": "child", "rows": 2}, "a" * 64, 2

    monkeypatch.setattr(removal, "review_table_removal", review)
    with pytest.raises(ValueError, match="Physical evidence changed before DDL"):
        removal.run_table_removal(args, connection, models, ".", "data", "identity", None)
    assert not any("DROP TABLE" in sql for sql, _ in connection.statements)


def test_mysql_drop_execution_error_is_uncertain(monkeypatch):
    connection, models, args = execution(monkeypatch, "mysql")
    answers = iter(["y", "DELETE TABLE data.child"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    original = connection.execute_write

    def fail_drop(sql, params=None):
        if sql.startswith("DROP TABLE"):
            raise OSError("Lost connection while executing DDL")
        return original(sql, params)

    connection.execute_write = fail_drop
    with pytest.raises(RuntimeError, match="outcome uncertain after DDL began"):
        removal.run_table_removal(args, connection, models, ".", "data", "identity", None)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_outgoing_foreign_key_is_not_treated_as_a_surviving_dependency(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect)
    original = removal.inspect_table(connection, "child")
    outgoing = (("user_id", "items", "id"),)
    indexes = original.indexes + (("user_id",) if dialect == "mysql" else ())

    monkeypatch.setattr(
        removal,
        "inspect_table",
        lambda *_: TableInfo(
            "child",
            True,
            original.columns,
            (),
            indexes,
            True,
            True,
            outgoing,
            (("user_id", "NO ACTION", "NO ACTION"),),
        ),
    )
    if dialect == "mysql":
        connection.fetch_all = lambda *args, **kwargs: [
            {
                "name": "user_id",
                "column_name": "user_id",
                "position": 1,
                "non_unique": 1,
                "index_type": "BTREE",
                "prefix_length": None,
            }
        ]

    operation, digest, count = removal.review_table_removal(
        connection,
        models,
        ".",
        "data",
        "identity",
    )
    assert operation["table"] == "child"
    assert count == 1
    assert len(digest) == 64


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_incoming_dependency_refuses_even_with_outgoing_foreign_key(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect, dependencies=True)
    original = removal.inspect_table(connection, "child")
    monkeypatch.setattr(
        removal,
        "inspect_table",
        lambda *_: TableInfo(
            "child",
            True,
            original.columns,
            (),
            original.indexes,
            True,
            True,
            (("user_id", "items", "id"),),
            (("user_id", "NO ACTION", "NO ACTION"),),
        ),
    )
    if dialect == "mysql":
        connection.fetch_all = lambda *args, **kwargs: []

    with pytest.raises(ValueError, match="Foreign-key dependencies from surviving"):
        removal.review_table_removal(connection, models, ".", "data", "identity")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_outgoing_constraint_catalog_must_match_verified_count(dialect):
    class ConstraintCatalog:
        def __init__(self):
            self.dialect = dialect

        def fetch_all(self, sql, params=None):
            return [{"kind": "PRIMARY KEY" if dialect == "mysql" else "p"}]

        def fetch_first(self, sql, params=None):
            if dialect == "postgresql":
                return {
                    "persistence": "p",
                    "partitioned": False,
                    "inherited": False,
                    "rls": False,
                    "triggers": 0,
                    "parents": 0,
                }
            return None

    with pytest.raises(ValueError, match="unsupported constraints"):
        removal._verify_removal_constraints(ConstraintCatalog(), "child", 1)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_incoming_dependency_query_only_checks_surviving_references(dialect):
    class DependencyCatalog:
        def __init__(self):
            self.dialect = dialect
            self.query = None

        def fetch_first(self, sql, params=None):
            self.query = sql
            return None

    connection = DependencyCatalog()
    assert not removal._incoming_foreign_key_dependency(connection, "child")
    if dialect == "mysql":
        assert "TABLE_NAME=:table" in connection.query
    else:
        assert "c.conrelid<>c.confrelid" in connection.query


def test_mixed_table_transition_preserves_existing_table_declarations():
    previous, current = schemas()
    replacement = {"name": "audit", "columns": [{"name": "id", "primary_key": True}]}

    assert removal.mixed_table_transition(previous, current + [replacement]) == (
        "child",
        ["audit"],
    )
    assert removal.mixed_table_transition(previous, current) is None
    assert removal.mixed_table_transition(previous, [replacement]) is None
    assert (
        removal.mixed_table_transition(
            previous,
            [
                {"name": "items", "columns": []},
                replacement,
            ],
        )
        is None
    )


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_mixed_review_requires_verified_parent_and_new_model(monkeypatch, dialect):
    connection, models = setup(monkeypatch, dialect, rows=0)
    original, surviving = schemas()
    replacement = {"name": "audit", "columns": [{"name": "id", "primary_key": True}]}
    models.append(SimpleNamespace(__table_name__="audit"))
    monkeypatch.setattr(
        removal,
        "read_history",
        lambda *args: (
            [{"id": 1}],
            {"models": original, "physical_schema": []},
        ),
    )
    monkeypatch.setattr(
        removal,
        "schema_snapshot",
        lambda *args, **kwargs: {
            "tables": surviving + [replacement],
        },
    )

    operation, digest, count = removal.review_table_removal(
        connection,
        models,
        ".",
        "data",
        "identity",
        allow_new_tables=True,
    )

    assert operation == {
        "kind": "drop_table",
        "table": "child",
        "rows": 0,
        "create_tables": ["audit"],
    }
    assert len(digest) == 64
    assert count == 1


def test_postgres_mixed_execution_creates_before_history(monkeypatch):
    connection, models, args = execution(monkeypatch, "postgresql", rows=0)
    models.append(SimpleNamespace(__table_name__="audit"))
    operation = {
        "kind": "drop_table",
        "table": "child",
        "rows": 0,
        "create_tables": ["audit"],
    }
    monkeypatch.setattr(
        removal,
        "review_table_removal",
        lambda *a, **k: (
            operation,
            "a" * 64,
            2,
        ),
    )
    monkeypatch.setattr(
        removal,
        "create_table_sql",
        lambda model, *a, **k: f"CREATE TABLE {model.__table_name__} (id INTEGER PRIMARY KEY)",
    )
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    assert (
        removal.run_table_removal(
            args,
            connection,
            models,
            ".",
            "data",
            "identity",
            None,
            allow_new_tables=True,
        )
        == 0
    )

    executed = [statement for statement, _ in connection.statements]
    assert (
        executed.index('DROP TABLE "child"')
        < executed.index("CREATE TABLE audit (id INTEGER PRIMARY KEY)")
        < next(i for i, statement in enumerate(executed) if statement.startswith("INSERT INTO"))
    )


def test_postgres_mixed_create_failure_does_not_write_history(monkeypatch):
    connection, models, args = execution(monkeypatch, "postgresql", rows=0)
    models.append(SimpleNamespace(__table_name__="audit"))
    operation = {
        "kind": "drop_table",
        "table": "child",
        "rows": 0,
        "create_tables": ["audit"],
    }
    monkeypatch.setattr(
        removal,
        "review_table_removal",
        lambda *a, **k: (
            operation,
            "a" * 64,
            2,
        ),
    )
    monkeypatch.setattr(removal, "create_table_sql", lambda *a, **k: "CREATE TABLE audit (id INT)")
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    original_execute = connection.execute_write

    def failing_create(statement, params=None):
        if statement.startswith("CREATE TABLE"):
            raise RuntimeError("CREATE failed")
        return original_execute(statement, params)

    connection.execute_write = failing_create

    with pytest.raises(RuntimeError, match="CREATE failed"):
        removal.run_table_removal(
            args,
            connection,
            models,
            ".",
            "data",
            "identity",
            None,
            allow_new_tables=True,
        )

    assert any(statement.startswith("DROP TABLE") for statement, _ in connection.statements)
    assert not any(statement.startswith("INSERT INTO") for statement, _ in connection.statements)


def test_mixed_review_rejects_new_reference_to_removed_table(monkeypatch):
    connection, models = setup(monkeypatch, "mysql", rows=0)
    old, survivors = schemas()
    models.append(SimpleNamespace(__table_name__="audit"))
    replacement = {
        "name": "audit",
        "columns": [{"name": "child_id", "references": "child.id"}],
    }
    monkeypatch.setattr(
        removal,
        "schema_snapshot",
        lambda *a, **k: {
            "tables": survivors + [replacement],
        },
    )

    with pytest.raises(ValueError, match="references the removed table"):
        removal.review_table_removal(
            connection,
            models,
            ".",
            "data",
            "identity",
            allow_new_tables=True,
        )


def test_mysql_mixed_creates_before_dropping_and_records_one_successor(monkeypatch, capsys):
    connection, models, args = execution(monkeypatch, "mysql", rows=0)
    models.append(SimpleNamespace(__table_name__="audit"))
    operation = {
        "kind": "drop_table",
        "table": "child",
        "rows": 0,
        "create_tables": ["audit"],
    }
    monkeypatch.setattr(
        removal,
        "review_table_removal",
        lambda *a, **k: (
            operation,
            "a" * 64,
            2,
        ),
    )
    monkeypatch.setattr(
        removal,
        "create_table_sql",
        lambda model, *a, **k: f"CREATE TABLE {model.__table_name__} (id INTEGER PRIMARY KEY)",
    )
    monkeypatch.setattr("builtins.input", lambda *_: "y")

    assert (
        removal.run_table_removal(
            args,
            connection,
            models,
            ".",
            "data",
            "identity",
            None,
            allow_new_tables=True,
        )
        == 0
    )

    executed = [sql for sql, _ in connection.statements]
    create = executed.index("CREATE TABLE audit (id INTEGER PRIMARY KEY)")
    drop = executed.index("DROP TABLE `child`")
    history = next(i for i, sql in enumerate(executed) if sql.startswith("INSERT INTO"))
    assert create < drop < history
    assert "not atomic" in capsys.readouterr().out


@pytest.mark.parametrize("failure_prefix", ["CREATE TABLE", "DROP TABLE"])
def test_mysql_mixed_partial_ddl_refuses_history_and_retry(monkeypatch, failure_prefix):
    connection, models, args = execution(monkeypatch, "mysql", rows=0)
    models.append(SimpleNamespace(__table_name__="audit"))
    operation = {
        "kind": "drop_table",
        "table": "child",
        "rows": 0,
        "create_tables": ["audit"],
    }
    monkeypatch.setattr(
        removal,
        "review_table_removal",
        lambda *a, **k: (
            operation,
            "a" * 64,
            2,
        ),
    )
    monkeypatch.setattr(
        removal, "create_table_sql", lambda *a, **k: "CREATE TABLE audit (id INTEGER PRIMARY KEY)"
    )
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    execute = connection.execute_write

    def fail_at_selected_ddl(sql, params=None):
        if sql.startswith(failure_prefix):
            raise OSError("DDL connection interrupted")
        return execute(sql, params)

    connection.execute_write = fail_at_selected_ddl

    with pytest.raises(RuntimeError, match="outcome uncertain after DDL began"):
        removal.run_table_removal(
            args,
            connection,
            models,
            ".",
            "data",
            "identity",
            None,
            allow_new_tables=True,
        )

    executed = [sql for sql, _ in connection.statements]
    assert not any(sql.startswith("INSERT INTO") for sql in executed)
    if failure_prefix == "CREATE TABLE":
        assert not any(sql.startswith("DROP TABLE") for sql in executed)
    else:
        assert any(sql.startswith("CREATE TABLE") for sql in executed)
