"""Server CLI reports verified data-loss refusals without exposing driver errors."""

from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import cli_init, migration_history, migration_review, snapshots
from aurora.database import cli_migrate as cli


@pytest.mark.parametrize("dialect", ("mysql", "postgresql"))
def test_unsafe_varchar_narrowing_reports_measured_length(monkeypatch, tmp_path, dialect):
    connection = SimpleNamespace(dialect=dialect)
    args = SimpleNamespace(
        database="data_mysql" if dialect == "mysql" else "data_postgres",
        title="Unsafe narrowing",
        apply=False,
        approve=None,
        backup=None,
        confirm=None,
    )
    monkeypatch.setattr(migration_history, "read_history", lambda *_: ([{"id": 1}], {"models": []}))
    monkeypatch.setattr(snapshots, "schema_snapshot", lambda *a, **k: {"tables": ["changed"]})
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_postgres_database_identity", lambda *_: "identity")
    monkeypatch.setattr(
        migration_review,
        "review_server_model_changes",
        lambda *_: (_ for _ in ()).throw(
            ValueError(
                "Cannot narrow mysql_item.category to VARCHAR(20): "
                "existing value length 28 exceeds the target"
            )
        ),
    )
    monkeypatch.setattr(
        migration_review,
        "review_server_column_widening",
        lambda *_: (_ for _ in ()).throw(
            ValueError("VARCHAR narrowing or unchanged length is not supported")
        ),
    )
    with pytest.raises(
        cli.UnsupportedServerChange,
        match=r"Cannot narrow mysql_item.category to VARCHAR\(20\): "
        r"existing value length 28 exceeds the target",
    ):
        cli._run_forward_migration(args, None, tmp_path, connection, (), object(), None)


def test_arbitrary_driver_error_does_not_reach_terminal(monkeypatch, tmp_path):
    connection = SimpleNamespace(dialect="mysql")
    args = SimpleNamespace(
        database="data_mysql", title="Unsafe", apply=False, approve=None, backup=None, confirm=None
    )
    monkeypatch.setattr(migration_history, "read_history", lambda *_: ([{"id": 1}], {"models": []}))
    monkeypatch.setattr(snapshots, "schema_snapshot", lambda *a, **k: {"tables": ["changed"]})
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *_: "identity")
    monkeypatch.setattr(
        migration_review,
        "review_server_model_changes",
        lambda *_: (_ for _ in ()).throw(ValueError("private connection string example")),
    )
    monkeypatch.setattr(
        migration_review,
        "review_server_column_widening",
        lambda *_: (_ for _ in ()).throw(ValueError("private driver detail")),
    )
    with pytest.raises(cli.UnsupportedServerChange) as exc:
        cli._run_forward_migration(args, None, tmp_path, connection, (), object(), None)
    assert "private" not in str(exc.value)
