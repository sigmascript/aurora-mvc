"""Public server migration dispatch for the five-column history format."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aurora.database import cli_server_model_migration as cli


@pytest.mark.parametrize("dialect,scheme", [
    ("mysql", "mysql+pymysql"), ("postgresql", "postgresql+psycopg"),
])
def test_forward_history_uses_forward_executor(monkeypatch, tmp_path, dialect, scheme):
    from aurora.database import server_forward_history

    class Config:
        def resolve(self, alias):
            return f"{scheme}://user@localhost/accounting"

        @contextmanager
        def connect(self, alias):
            yield SimpleNamespace(dialect=dialect, fetch_all=lambda *args: [])

    connection_models = [object()]
    monkeypatch.setattr(cli, "_load_config", lambda path: (Config(), tmp_path))
    monkeypatch.setattr(cli, "_select_models", lambda args, root: ([], connection_models))
    monkeypatch.setattr(server_forward_history, "has_history", lambda conn: True)
    monkeypatch.setattr(server_forward_history, "read_history", lambda *args: ([{}], {}))
    seen = []
    monkeypatch.setattr(cli, "_run_forward_migration", lambda *args, **kwargs: seen.append((args, kwargs)) or 0)

    args = SimpleNamespace(config="config", database="accounting", apply=True,
                           confirm="APPLY", approve="a" * 64, backup="backup.sql",
                           name="Migration2", login_path=None)
    assert cli.run_server_model_migration(args) == 0
    assert len(seen) == 1
    assert seen[0][0][4] == connection_models
    assert seen[0][1]["verified_history"] == ([{}], {})

@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_forward_widening_uses_existing_safe_reviewer(monkeypatch, tmp_path, dialect):
    """A verified column widening keeps the same five-column execution path."""
    from aurora.database import cli_init, server_column_widening
    from aurora.database import server_forward_history, server_forward_migration
    from aurora.database import server_model_review

    proposal = SimpleNamespace(
        operations=({"kind": "widen_column", "table": "notes", "column": "body"},),
        statements=("ALTER TABLE notes ...",),
        review_sha256="a" * 64,
    )

    def reject_additive(*args):
        raise ValueError("Not an additive change")

    monkeypatch.setattr(server_model_review, "review_server_model_changes", reject_additive)
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *args: proposal)
    monkeypatch.setattr(server_forward_history, "read_history", lambda *args: ([{}], {}))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "identity")

    executions = []

    def execute(*args, **kwargs):
        executions.append(kwargs)
        return tmp_path / "snapshot.json", tmp_path / "backup.dump"

    monkeypatch.setattr(server_forward_migration, "execute_server_forward_migration", execute)
    (tmp_path / "backups").mkdir()
    args = SimpleNamespace(database="data", config="config", confirm="yes",
                           apply=False, approve=None, title="Widen body")
    config = SimpleNamespace()
    connection = SimpleNamespace(dialect=dialect)

    assert cli._run_forward_migration(args, config, tmp_path, connection,
                                      [object()], object(), None) == 0
    assert executions[0]["review"] is proposal
    assert executions[0]["approval"] == proposal.review_sha256


def test_forward_widening_rejects_unreviewable_drift(monkeypatch, tmp_path):
    """Neither reviewer may turn an unsupported change into DDL."""
    from aurora.database import cli_init, server_column_widening
    from aurora.database import server_forward_history, server_model_review

    monkeypatch.setattr(server_forward_history, "read_history", lambda *args: ([{}], {}))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "identity")
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *args: (_ for _ in ()).throw(ValueError("Unsupported change")))
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *args: (_ for _ in ()).throw(ValueError("Unsafe widening")))
    args = SimpleNamespace(database="data", confirm="yes", apply=False, approve=None)

    with pytest.raises(cli.UnsupportedServerChange, match="Only one new table"):
        cli._run_forward_migration(args, None, tmp_path,
                                   SimpleNamespace(dialect="mysql"), [object()],
                                   object(), None)

@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_unchanged_forward_schema_is_verified_noop(monkeypatch, tmp_path, dialect, capsys):
    """An unchanged physical/history witness must not create another migration."""
    from aurora.database import server_forward_history, server_model_review
    from aurora.database.snapshots import schema_snapshot
    from tests.database.test_schema import Entry

    models = [Entry]
    latest = {"models": schema_snapshot(*models, dialect=dialect)["tables"]}
    monkeypatch.setattr(server_forward_history, "read_history",
                        lambda *args: ([{"id": 1}], latest))
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *args: pytest.fail("No review needed for unchanged models"))

    args = SimpleNamespace(database="data", apply=False, confirm="yes")
    connection = SimpleNamespace(dialect=dialect)
    assert cli._run_forward_migration(args, None, tmp_path, connection,
                                      models, object(), None) == 0
    assert "up to date" in capsys.readouterr().out


def test_changed_forward_schema_does_not_use_noop(monkeypatch, tmp_path):
    """Different declarations still pass through refusal or reviewed execution."""
    from aurora.database import server_forward_history, server_model_review
    from tests.database.test_schema import Entry

    monkeypatch.setattr(server_forward_history, "read_history",
                        lambda *args: ([{"id": 1}], {"models": []}))
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *args: (_ for _ in ()).throw(ValueError("Change requires review")))
    from aurora.database import server_column_widening
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *args: (_ for _ in ()).throw(ValueError("Unsupported change")))
    from aurora.database import cli_init
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "identity")

    with pytest.raises(cli.UnsupportedServerChange, match="Only one new table"):
        cli._run_forward_migration(SimpleNamespace(database="data", confirm="yes"),
                                   None, tmp_path, SimpleNamespace(dialect="mysql"),
                                   [Entry], object(), None)

@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_ordinary_forward_migration_does_not_request_backup(monkeypatch, tmp_path, dialect):
    """The five-column CLI must not silently create mandatory backup files."""
    from aurora.database import cli_init, server_forward_history, server_model_review
    from aurora.database import server_forward_migration

    proposal = SimpleNamespace(
        operations=({"kind": "create_table", "table": "notes"},),
        statements=("CREATE TABLE notes (id INT)",),
        review_sha256="a" * 64,
    )
    monkeypatch.setattr(server_forward_history, "read_history", lambda *args: ([{"id": 1}], {}))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "identity")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "identity")
    monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *args: proposal)

    seen = []

    def execute(*args, **kwargs):
        seen.append(kwargs)
        return tmp_path / "new.json", None

    monkeypatch.setattr(server_forward_migration, "execute_server_forward_migration", execute)
    args = SimpleNamespace(database="data", config="config", confirm="yes",
                           apply=False, approve=None, title="Add notes")

    assert cli._run_forward_migration(
        args, None, tmp_path, SimpleNamespace(dialect=dialect),
        [object()], object(), None,
    ) == 0
    assert seen[0]["backup_path"] is None
    assert not (tmp_path / "backups").exists()

@pytest.mark.parametrize("dialect,scheme", [
    ("mysql", "mysql+pymysql"),
    ("postgresql", "postgresql+psycopg"),
])
def test_plain_noninteractive_verified_noop_needs_no_confirmation(
        monkeypatch, tmp_path, dialect, scheme, capsys):
    """A verified no-op must not request approval or start a migration."""
    from aurora.database import server_forward_history
    from aurora.database.snapshots import schema_snapshot
    from tests.database.test_schema import Entry

    class Config:
        def resolve(self, alias):
            return f"{scheme}://user@localhost/accounting"

        @contextmanager
        def connect(self, alias):
            yield SimpleNamespace(dialect=dialect, fetch_all=lambda *args: [])

    monkeypatch.setattr(cli, "_load_config", lambda path: (Config(), tmp_path))
    monkeypatch.setattr(cli, "_select_models", lambda args, root: ([], [Entry]))
    monkeypatch.setattr(server_forward_history, "has_history", lambda connection: True)
    latest = {"models": schema_snapshot(Entry, dialect=dialect)["tables"]}
    monkeypatch.setattr(server_forward_history, "read_history",
                        lambda *args: ([{"id": 2}], latest))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "_run_forward_migration",
                        lambda *args: pytest.fail("No migration execution for a no-op"))

    args = SimpleNamespace(config="config", database="accounting", confirm=None,
                           apply=False, login_path=None)
    assert cli.run_server_model_migration(args) == 0
    assert "up to date" in capsys.readouterr().out


@pytest.mark.parametrize("dialect,scheme", [
    ("mysql", "mysql+pymysql"),
    ("postgresql", "postgresql+psycopg"),
])
def test_plain_noninteractive_changed_schema_still_requires_approval(
        monkeypatch, tmp_path, dialect, scheme):
    """Only unchanged verified schemas may bypass terminal confirmation."""
    from aurora.database import server_forward_history
    from tests.database.test_schema import Entry

    class Config:
        def resolve(self, alias):
            return f"{scheme}://user@localhost/accounting"

        @contextmanager
        def connect(self, alias):
            yield SimpleNamespace(dialect=dialect, fetch_all=lambda *args: [])

    monkeypatch.setattr(cli, "_load_config", lambda path: (Config(), tmp_path))
    monkeypatch.setattr(cli, "_select_models", lambda args, root: ([], [Entry]))
    monkeypatch.setattr(server_forward_history, "has_history", lambda connection: True)
    monkeypatch.setattr(server_forward_history, "read_history",
                        lambda *args: ([{"id": 2}], {"models": []}))
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "_run_forward_migration",
                        lambda *args: pytest.fail("Changed schema must not execute"))

    args = SimpleNamespace(config="config", database="accounting", confirm=None,
                           apply=False, login_path=None)
    with pytest.raises(PermissionError, match="requires a terminal"):
        cli.run_server_model_migration(args)


@pytest.mark.parametrize("reason,expected", [
    ("VARCHAR narrowing or unchanged length is not supported", "VARCHAR narrowing"),
    ("DECIMAL precision/scale cannot be reduced", "DECIMAL narrowing"),
    ("password=secret hostile driver error", "Only one new table"),
])
def test_unsupported_forward_change_uses_safe_specific_reason(
        monkeypatch, tmp_path, reason, expected):
    """Only approved reviewer explanations may reach the public CLI."""
    from aurora.database import cli_init, server_column_widening
    from aurora.database import server_forward_history, server_model_review

    monkeypatch.setattr(server_forward_history, "read_history", lambda *args: ([{}], {}))
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "identity")
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *args: (_ for _ in ()).throw(ValueError("Not additive")))
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *args: (_ for _ in ()).throw(ValueError(reason)))
    args = SimpleNamespace(database="data", confirm="yes", apply=False, approve=None)

    with pytest.raises(cli.UnsupportedServerChange, match=expected) as refusal:
        cli._run_forward_migration(args, None, tmp_path,
                                   SimpleNamespace(dialect="mysql"), [object()],
                                   object(), None)

    assert "secret" not in str(refusal.value)


def test_public_migrate_explains_safe_refusal_without_driver_details(monkeypatch, capsys):
    """The CLI shows reviewed reasons without exposing untrusted exception text."""
    from aurora.cli import main
    from aurora.database import cli_model_migration

    def refuse(*args):
        raise cli.UnsupportedServerChange(
            "VARCHAR narrowing is not supported; preserve or increase the length."
        )

    monkeypatch.setattr(cli_model_migration, "run_model_migration", refuse)
    with pytest.raises(SystemExit) as exit_status:
        main(["db", "migrate", "--confirm", "yes"])

    assert exit_status.value.code == 1
    output = capsys.readouterr().err
    assert "VARCHAR narrowing is not supported" in output
    assert "ValueError" not in output

@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_forward_dispatch_reuses_verified_history_without_second_read(
        monkeypatch, tmp_path, dialect, capsys):
    """A verified dispatch witness is reused, not read a second time."""
    from aurora.database import server_forward_history, server_model_review
    from aurora.database.snapshots import schema_snapshot
    from tests.database.test_schema import Entry

    models = [Entry]
    history = ([{"id": 1}], {"models": schema_snapshot(Entry, dialect=dialect)["tables"]})
    monkeypatch.setattr(server_forward_history, "read_history",
                        lambda *args: pytest.fail("Duplicate history read"))
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *args: pytest.fail("No review for unchanged models"))
    args = SimpleNamespace(database="data", apply=False, confirm="yes")
    connection = SimpleNamespace(dialect=dialect)
    assert cli._run_forward_migration(args, None, tmp_path, connection,
                                      models, object(), None, verified_history=history) == 0
    assert "up to date" in capsys.readouterr().out
