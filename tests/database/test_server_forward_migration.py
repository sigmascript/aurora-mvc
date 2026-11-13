"""Guarded five-column server successor tests without live DDL."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aurora.database import server_forward_migration as successor


class Connection:
    def __init__(self, dialect):
        self.dialect = dialect
        self._transaction_active = False
        self.statements = []
        self.lock_available = True

    def fetch_first(self, sql, params=None):
        return {"acquired": self.lock_available}

    def execute_write(self, sql, params=None):
        self.statements.append(sql)
        if sql.startswith("INSERT INTO migrations"):
            self.last_record = params

    @contextmanager
    def transaction(self):
        yield


@contextmanager
def unlocked(_connection):
    yield


def setup(monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init, cli_mysql_backup, cli_postgres_backup
    from aurora.database import server_model_review

    connection = Connection(dialect)
    proposal = SimpleNamespace(
        operations=({"kind": "create_table", "table": "notes"},),
        statements=("CREATE TABLE notes (id INT)",),
        review_sha256="a" * 64,
    )
    archive = tmp_path / "verified.dump"
    archive.write_bytes(b"archive")

    monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", lambda *args: archive)
    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", lambda *args: archive)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "identity")
    monkeypatch.setattr(successor, "migration_lock", unlocked)
    monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *args: proposal)
    monkeypatch.setattr(successor, "review_server_model_changes", lambda *args: proposal)
    monkeypatch.setattr(successor, "verify_server_declared_schema", lambda *args, **kwargs: None)
    monkeypatch.setattr(successor, "_write_snapshot", lambda *args: tmp_path / "new.json")
    def recorded_history(*args):
        if len(connection.statements) < 2:
            return ([{"id": 1}], {})
        return ([{"id": 1}, {"id": 2, **connection.last_record}], {})

    monkeypatch.setattr(successor, "read_history", recorded_history)

    args = dict(identity="identity", parsed=object(), review=proposal,
                approval="a" * 64, title="Add notes", backup_path="backup",
                config_path="config")
    return connection, args, archive


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_successor_writes_one_change_and_one_forward_record(monkeypatch, tmp_path, dialect):
    connection, args, archive = setup(monkeypatch, tmp_path, dialect)
    snapshot, returned_archive = successor.execute_server_forward_migration(
        connection, [object()], tmp_path, "data", **args)

    assert snapshot.name == "new.json"
    assert returned_archive == archive
    assert connection.statements == [
        "CREATE TABLE notes (id INT)",
        "INSERT INTO migrations (id,title,snapshot,date,warnings) "
        "VALUES (:id,:title,:snapshot,:date,:warnings)",
    ]


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_bad_approval_refused_before_backup_or_ddl(monkeypatch, tmp_path, dialect):
    connection, args, _ = setup(monkeypatch, tmp_path, dialect)
    args["approval"] = "b" * 64
    with pytest.raises(PermissionError, match="approved"):
        successor.execute_server_forward_migration(connection, [], tmp_path, "data", **args)
    assert not connection.statements


def test_postgres_lock_failure_refuses_before_ddl(monkeypatch, tmp_path):
    connection, args, _ = setup(monkeypatch, tmp_path, "postgresql")
    connection.lock_available = False
    with pytest.raises(RuntimeError, match="lock unavailable"):
        successor.execute_server_forward_migration(connection, [], tmp_path, "data", **args)
    assert not connection.statements


def test_review_changes_under_lock_refused_before_ddl(monkeypatch, tmp_path):
    connection, args, _ = setup(monkeypatch, tmp_path, "mysql")
    changed = SimpleNamespace(operations=args["review"].operations,
                              statements=("DIFFERENT DDL",), review_sha256="b" * 64)
    monkeypatch.setattr(successor, "review_server_model_changes", lambda *args: changed)
    with pytest.raises(RuntimeError, match="changed after review"):
        successor.execute_server_forward_migration(connection, [], tmp_path, "data", **args)
    assert not connection.statements


def test_mysql_history_write_failure_is_not_retried(monkeypatch, tmp_path):
    connection, args, _ = setup(monkeypatch, tmp_path, "mysql")
    original = connection.execute_write

    def execute(sql, params=None):
        if sql.startswith("INSERT INTO migrations"):
            raise RuntimeError("uncertain journal write")
        original(sql, params)

    connection.execute_write = execute
    with pytest.raises(RuntimeError, match="outcome uncertain after DDL") as error:
        successor.execute_server_forward_migration(connection, [], tmp_path, "data", **args)
    assert str(error.value.__cause__) == "uncertain journal write"
    assert connection.statements == ["CREATE TABLE notes (id INT)"]

@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_reviewed_widening_uses_identical_guarded_executor(monkeypatch, tmp_path, dialect):
    connection, args, archive = setup(monkeypatch, tmp_path, dialect)
    proposal = SimpleNamespace(
        operations=({"kind": "widen_column", "table": "notes", "column": "body"},),
        statements=("ALTER TABLE notes WIDEN body",),
        review_sha256="b" * 64,
    )
    monkeypatch.setattr(successor, "review_server_column_widening", lambda *args: proposal)
    args.update(review=proposal, approval=proposal.review_sha256)

    snapshot, returned_archive = successor.execute_server_forward_migration(
        connection, [object()], tmp_path, "data", **args)

    assert snapshot.name == "new.json"
    assert returned_archive == archive
    assert connection.statements[0] == proposal.statements[0]
    assert connection.statements[1].startswith("INSERT INTO migrations")

@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_optional_backup_does_not_invoke_backup_client(monkeypatch, tmp_path, dialect):
    """A normal forward migration needs no backup credentials or archive."""
    from aurora.database import cli_mysql_backup, cli_postgres_backup

    connection, args, _ = setup(monkeypatch, tmp_path, dialect)
    args["backup_path"] = None

    def unexpected_backup(*args):
        pytest.fail("Normal forward migration must not invoke backup tools")

    monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", unexpected_backup)
    monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", unexpected_backup)

    snapshot, archive = successor.execute_server_forward_migration(
        connection, [object()], tmp_path, "data", **args
    )

    assert snapshot.name == "new.json"
    assert archive is None
    assert len(connection.statements) == 2


@pytest.mark.parametrize("failure", ["ddl", "verification", "snapshot", "history"])
def test_mysql_failures_after_ddl_begins_require_reconciliation(
    monkeypatch, tmp_path, failure
):
    connection, args, _ = setup(monkeypatch, tmp_path, "mysql")
    snapshot = tmp_path / "existing-evidence.json"
    snapshot.write_text('{"preserved": true}', encoding="utf-8")

    original_execute = connection.execute_write

    def execute(sql, params=None):
        if failure == "ddl" and sql.startswith("CREATE TABLE"):
            raise OSError("connection lost during DDL")
        if failure == "history" and sql.startswith("INSERT INTO migrations"):
            raise OSError("history insert uncertain")
        original_execute(sql, params)

    connection.execute_write = execute

    if failure == "verification":
        monkeypatch.setattr(
            successor, "verify_server_declared_schema",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("drift")),
        )
    if failure == "snapshot":
        monkeypatch.setattr(
            successor, "_write_snapshot",
            lambda *args: (_ for _ in ()).throw(OSError("disk full")),
        )

    with pytest.raises(RuntimeError, match="inspect physical schema.*before any retry") as error:
        successor.execute_server_forward_migration(
            connection, [object()], tmp_path, "data", **args
        )

    assert error.value.__cause__ is not None
    assert snapshot.read_text(encoding="utf-8") == '{"preserved": true}'
    assert len(connection.statements) <= 1


def test_postgresql_failure_propagates_inside_transaction(monkeypatch, tmp_path):
    connection, args, _ = setup(monkeypatch, tmp_path, "postgresql")

    def failed_ddl(sql, params=None):
        raise RuntimeError("PostgreSQL transactional DDL failed")

    connection.execute_write = failed_ddl
    with pytest.raises(RuntimeError, match="transactional DDL failed"):
        successor.execute_server_forward_migration(
            connection, [object()], tmp_path, "data", **args
        )


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_successor_rejects_wrong_completed_history(monkeypatch, tmp_path, dialect):
    connection, args, _ = setup(monkeypatch, tmp_path, dialect)

    def altered_history(*unused):
        if len(connection.statements) < 2:
            return ([{"id": 1}], {})
        return ([{"id": 1}, {"id": 2, **connection.last_record,
                             "snapshot": "different.json"}], {})

    monkeypatch.setattr(successor, "read_history", altered_history)
    expected = "outcome uncertain" if dialect == "mysql" else "completion was not verified"
    with pytest.raises(RuntimeError, match=expected):
        successor.execute_server_forward_migration(
            connection, [object()], tmp_path, "data", **args
        )
    assert len(connection.statements) == 2
