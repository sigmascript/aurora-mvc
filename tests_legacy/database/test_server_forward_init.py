"""Focused forward server initialization boundaries; no real server DDL."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_init as forward


class Connection:
    def __init__(self, dialect):
        self.dialect = dialect
        self._transaction_active = False
        self.statements = []
        self.committed = False

    def _ensure_open(self):
        pass

    def execute_write(self, sql, params=None):
        self.statements.append(sql)
        if sql.startswith("INSERT INTO migrations"):
            self.last_record = params

    def fetch_first(self, sql, params=None):
        return {"acquired": True}

    @contextmanager
    def transaction(self):
        try:
            yield self
        except BaseException:
            self.committed = False
            raise
        else:
            self.committed = True


def _configure(monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init, init_validation
    connection = Connection(dialect)
    model = SimpleNamespace(__table_name__="note")
    monkeypatch.setattr(init_validation, "validate_init_models", lambda models, dialect: models)
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda conn, parsed: "identity")
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda conn, parsed: "identity")
    monkeypatch.setattr(cli_init, "_postgres_approval_digest", lambda models, alias, identity: "a" * 64)
    monkeypatch.setattr(cli_init, "_mysql_approval_digest", lambda models, alias, identity: "a" * 64)
    monkeypatch.setattr(forward, "migration_lock", lambda conn: _lock())
    return connection, [model], {"approval": "a" * 64,
                                 "expected_approval": "a" * 64,
                                 "expected_identity": "identity", "parsed": object()}


@contextmanager
def _lock():
    yield


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_review_failure_never_attempts_ddl(monkeypatch, tmp_path, dialect):
    connection, models, opts = _configure(monkeypatch, tmp_path, dialect)
    opts["approval"] = "b" * 64
    with pytest.raises(PermissionError):
        forward.execute_server_forward_init(connection, models, tmp_path, "data", **opts)
    assert connection.statements == []


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_identity_mismatch_never_attempts_ddl(monkeypatch, tmp_path, dialect):
    connection, models, opts = _configure(monkeypatch, tmp_path, dialect)
    opts["expected_identity"] = "wrong"
    with pytest.raises(RuntimeError, match="identity"):
        forward.execute_server_forward_init(connection, models, tmp_path, "data", **opts)
    assert connection.statements == []


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_initializer_preserves_guarded_ddl_order(monkeypatch, tmp_path, dialect):
    connection, models, opts = _configure(monkeypatch, tmp_path, dialect)
    from aurora.database import cli_init, server_initial_verification
    monkeypatch.setattr(cli_init, "_preflight", lambda *a: {"status": "EMPTY", "review_warnings": []})
    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema", lambda *a, **kw: None)
    monkeypatch.setattr(forward, "create_table_sql", lambda model, dialect: "CREATE TABLE note (id INT)")
    monkeypatch.setattr(forward, "_write_snapshot", lambda *args: tmp_path / "snapshot.json")
    monkeypatch.setattr(
        forward, "read_history",
        lambda *args: ([{"id": 1, **connection.last_record}], {}),
    )
    path = forward.execute_server_forward_init(connection, models, tmp_path, "data", **opts)
    assert path.name == "snapshot.json"
    assert connection.statements[0].startswith("CREATE TABLE migrations (")
    assert connection.statements[1].startswith("CREATE TABLE note (")
    assert connection.statements[2].startswith("INSERT INTO migrations (")
    assert connection.committed == (dialect == "postgresql")


def test_mysql_partial_ddl_failure_never_retries_or_cleans_tables(monkeypatch, tmp_path):
    connection, models, opts = _configure(monkeypatch, tmp_path, "mysql")
    from aurora.database import cli_init
    monkeypatch.setattr(cli_init, "_preflight", lambda *a: {"status": "EMPTY", "review_warnings": []})
    def fail_ddl(model, dialect):
        raise RuntimeError("unsafe rendering")
    monkeypatch.setattr(forward, "create_table_sql", fail_ddl)
    with pytest.raises(RuntimeError, match="outcome uncertain after DDL began") as error:
        forward.execute_server_forward_init(connection, models, tmp_path, "data", **opts)
    assert str(error.value.__cause__) == "unsafe rendering"
    assert len(connection.statements) == 1
    assert connection.statements[0].startswith("CREATE TABLE migrations")


def test_snapshot_writer_does_not_remove_file_after_write_failure(monkeypatch, tmp_path):
    from pathlib import Path

    connection = SimpleNamespace(dialect="mysql")
    model = SimpleNamespace(__table_name__="note")
    monkeypatch.setattr(forward, "schema_snapshot", lambda *args, **kwargs: {"tables": []})
    monkeypatch.setattr(forward, "_physical", lambda conn: [{"table": "note"}])

    original_open = Path.open

    def failing_open(path, *args, **kwargs):
        if path.suffix == ".json" and args and args[0] == "x":
            with original_open(path, "w", encoding="utf-8") as output:
                output.write("partial witness")
            raise OSError("disk write uncertain")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", failing_open)

    with pytest.raises(OSError, match="disk write uncertain"):
        forward._write_snapshot(connection, tmp_path, "data", [model], "Initial")

    snapshots = list((tmp_path / "migrations/data").glob("*.json"))
    assert len(snapshots) == 1
    assert snapshots[0].read_text(encoding="utf-8") == "partial witness"


@pytest.mark.parametrize("failure", ["ddl", "snapshot", "history"])
def test_mysql_initialization_failure_keeps_evidence_and_reports_uncertainty(
    monkeypatch, tmp_path, failure
):
    from aurora.database import cli_init, server_initial_verification

    connection, models, options = _configure(monkeypatch, tmp_path, "mysql")
    monkeypatch.setattr(
        cli_init, "_preflight",
        lambda *args: {"status": "EMPTY", "review_warnings": []},
    )
    monkeypatch.setattr(
        server_initial_verification, "verify_server_declared_schema",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(forward, "create_table_sql", lambda *args: "CREATE TABLE note (id INT)")

    snapshot = tmp_path / "preserved.json"
    snapshot.write_text("evidence", encoding="utf-8")
    monkeypatch.setattr(forward, "_write_snapshot", lambda *args: snapshot)
    monkeypatch.setattr(forward, "read_history", lambda *args: ([], {}))
    original_execute = connection.execute_write

    def execute(sql, params=None):
        if failure == "ddl" and "CREATE TABLE note" in sql:
            raise OSError("DDL outcome unknown")
        if failure == "history" and sql.startswith("INSERT INTO migrations"):
            raise OSError("history outcome unknown")
        original_execute(sql, params)

    connection.execute_write = execute

    if failure == "snapshot":
        monkeypatch.setattr(
            forward, "_write_snapshot",
            lambda *args: (_ for _ in ()).throw(OSError("disk unavailable")),
        )

    with pytest.raises(RuntimeError, match="inspect physical schema.*before any retry") as error:
        forward.execute_server_forward_init(
            connection, models, tmp_path, "data", **options
        )

    assert isinstance(error.value.__cause__, OSError)
    assert snapshot.read_text(encoding="utf-8") == "evidence"
    assert connection.statements[0].startswith("CREATE TABLE migrations")


def test_postgres_initialization_propagates_transaction_failure(monkeypatch, tmp_path):
    from aurora.database import cli_init

    connection, models, options = _configure(monkeypatch, tmp_path, "postgresql")
    monkeypatch.setattr(
        cli_init, "_preflight",
        lambda *args: {"status": "EMPTY", "review_warnings": []},
    )
    original_execute = connection.execute_write

    def execute(sql, params=None):
        if sql.startswith("CREATE TABLE migrations"):
            raise OSError("transactional DDL rejected")
        original_execute(sql, params)

    connection.execute_write = execute

    with pytest.raises(OSError, match="transactional DDL rejected"):
        forward.execute_server_forward_init(
            connection, models, tmp_path, "data", **options
        )

    assert connection.committed is False


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_initializer_rejects_mismatched_completed_history(monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init, server_initial_verification

    connection, models, options = _configure(monkeypatch, tmp_path, dialect)
    monkeypatch.setattr(
        cli_init, "_preflight",
        lambda *args: {"status": "EMPTY", "review_warnings": []},
    )
    monkeypatch.setattr(
        server_initial_verification, "verify_server_declared_schema",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(forward, "create_table_sql", lambda *args: "CREATE TABLE note (id INT)")
    monkeypatch.setattr(forward, "_write_snapshot", lambda *args: tmp_path / "snapshot.json")
    monkeypatch.setattr(
        forward, "read_history",
        lambda *args: ([{"id": 1, **connection.last_record,
                         "title": "unexpected title"}], {}),
    )

    message = "outcome uncertain" if dialect == "mysql" else "completion was not verified"
    with pytest.raises(RuntimeError, match=message):
        forward.execute_server_forward_init(connection, models, tmp_path, "data", **options)
    assert len(connection.statements) == 3
    if dialect == "postgresql":
        assert connection.committed is False
