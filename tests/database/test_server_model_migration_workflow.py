"""Review/alias routing tests for the still-internal server migration coordinator."""

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aurora.database import Model, column


class ExistingLedger(Model):
    __table__ = "existing_ledgers"
    id = column(int, primary_key=True)


class AddedInvoice(Model):
    __table__ = "added_invoices"
    id = column(int, primary_key=True)


def configure_workflow(monkeypatch, tmp_path, dialect):
    from aurora.database import cli_init, cli_server_model_migration
    from aurora.database import history, server_migration_timeline, server_model_review
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    models = [ExistingLedger, AddedInvoice]
    proposal = ServerModelProposal(dialect, ("CREATE TABLE added_invoices",),
                                   ({"kind": "create_table", "table": "added_invoices"},),
                                   "a" * 64)
    connection = SimpleNamespace(dialect=dialect)
    suffix = "mysql+pymysql" if dialect == "mysql" else "postgresql+psycopg"

    class Config:
        def resolve(self, alias):
            assert alias == "accounting"
            return f"{suffix}://aurora_user@localhost/accounting"

        @contextmanager
        def connect(self, alias):
            assert alias == "accounting"
            yield connection

    monkeypatch.setattr(cli_server_model_migration, "_load_config", lambda _: (Config(), tmp_path))
    monkeypatch.setattr(cli_server_model_migration, "_select_models",
                        lambda args, root: ([], models))
    monkeypatch.setattr(history, "review_history", lambda *_: ())
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "accounting", "b" * 64,
                             model_identity_map(models[:1]), "c" * 64, "applied"),))
    monkeypatch.setattr(server_migration_timeline, "verify_server_initial_current",
                        lambda *args: True)
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *args: proposal)
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *args: "accounting-id")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *args: "accounting-id")
    return connection


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_advanced_server_workflow_routes_named_alias_and_review(monkeypatch, tmp_path, dialect):
    from aurora.database import cli_server_model_migration, mysql_successor, postgres_successor

    configure_workflow(monkeypatch, tmp_path, dialect)
    calls = []

    def execute(connection, models, **kwargs):
        calls.append(kwargs)
        return "op", Path(kwargs["backup_path"])

    target = mysql_successor if dialect == "mysql" else postgres_successor
    name = ("execute_mysql_new_table_successor" if dialect == "mysql" else
            "execute_postgres_new_table_successor")
    monkeypatch.setattr(target, name, execute)
    backup = str(tmp_path / ("accounting.sql" if dialect == "mysql" else "accounting.dump"))
    args = SimpleNamespace(config="ignored", database="accounting", title="Add invoice table",
                           apply=True, confirm="APPLY", approve="a" * 64, backup=backup,
                           name="Invoices", output_dir="migrations", dry_run=False,
                           check=False, explain=False, format="text",
                           login_path="backup_profile" if dialect == "mysql" else None)

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert len(calls) == 1
    assert calls[0]["alias"] == "accounting"
    assert calls[0]["title"] == "Add invoice table"
    assert calls[0]["name"] == "Invoices"
    assert calls[0]["backup_path"] == backup
    if dialect == "mysql":
        assert calls[0]["login_path"] == "backup_profile"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_interactive_repeated_server_migration_uses_next_sequence(monkeypatch, tmp_path,
                                                                   dialect):
    from aurora.database import (cli_server_model_migration, mysql_successor,
                                 postgres_successor, server_history_chain,
                                 server_migration_timeline)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    configure_workflow(monkeypatch, tmp_path, dialect)
    previous = model_identity_map([ExistingLedger])
    timeline = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                             "b" * 64, previous, "c" * 64, "applied"),
        ServerMigrationEntry(2, "Migration2", "Prior edit", "accounting",
                             "d" * 64, previous, "e" * 64, "applied"),
    )
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *_: timeline)
    monkeypatch.setattr(server_history_chain, "verify_server_review_predecessor",
                        lambda *_: 3)
    monkeypatch.setattr(cli_server_model_migration.sys, "stdin",
                        SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    calls = []

    def execute(connection, models, **kwargs):
        calls.append(kwargs)
        return "operation", Path(kwargs["backup_path"])

    target = mysql_successor if dialect == "mysql" else postgres_successor
    executor = ("execute_mysql_new_table_successor" if dialect == "mysql" else
                "execute_postgres_new_table_successor")
    monkeypatch.setattr(target, executor, execute)
    args = SimpleNamespace(config="ignored", database="accounting", title="Add invoice",
                           apply=False, approve=None, backup=None, confirm=None, name=None)

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert len(calls) == 1
    assert calls[0]["name"] == "Migration3"
    assert calls[0]["alias"] == "accounting"


def test_server_workflow_refuses_mysql_profile_for_postgres(monkeypatch, tmp_path):
    from aurora.database import cli_server_model_migration

    configure_workflow(monkeypatch, tmp_path, "postgresql")
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=True, confirm="APPLY", approve="a" * 64,
                           backup=str(tmp_path / "before.dump"), name="Invoices",
                           login_path="backup_profile")

    with pytest.raises(ValueError, match="not supported by PostgreSQL"):
        cli_server_model_migration.run_server_model_migration(args)


def test_server_workflow_refuses_incorrect_advanced_approval(monkeypatch, tmp_path):
    from aurora.database import cli_server_model_migration

    configure_workflow(monkeypatch, tmp_path, "postgresql")
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=True, confirm="APPLY", approve="b" * 64,
                           backup=str(tmp_path / "before.dump"), name="Invoices")

    with pytest.raises(PermissionError, match="approval"):
        cli_server_model_migration.run_server_model_migration(args)


def test_interactive_server_migration_cancellation_does_not_create_backup(monkeypatch,
                                                                          tmp_path):
    from aurora.database import cli_server_model_migration

    configure_workflow(monkeypatch, tmp_path, "postgresql")
    monkeypatch.setattr(cli_server_model_migration.sys, "stdin",
                        SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=False, confirm=None, approve=None, backup=None, name=None)

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert not (tmp_path / "backups").exists()


def test_server_workflow_refuses_production_before_database_access(monkeypatch):
    from aurora.database import cli_server_model_migration

    monkeypatch.setenv("AURORA_ENV", "production")
    monkeypatch.setattr(cli_server_model_migration, "_load_config",
                        lambda *_: pytest.fail("Must not read production database"))

    with pytest.raises(PermissionError, match="production"):
        cli_server_model_migration.run_server_model_migration(SimpleNamespace())


@pytest.mark.parametrize("failure", ["unresolved_history", "unsupported_operation",
                                      "unverified_initial"])
def test_server_workflow_refuses_untrusted_review_before_executor(monkeypatch, tmp_path,
                                                                   failure):
    from aurora.database import (cli_server_model_migration, history, mysql_successor,
                                 server_migration_timeline, server_model_review)
    from aurora.database.server_model_review import ServerModelProposal

    configure_workflow(monkeypatch, tmp_path, "mysql")
    if failure == "unresolved_history":
        monkeypatch.setattr(history, "review_history", lambda *_: (object(),))
    elif failure == "unsupported_operation":
        monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *_:
                            ServerModelProposal("mysql", ("ALTER TABLE existing_ledgers",),
                                                ({"kind": "drop_column", "table": "existing_ledgers",
                                                  "column": "note"},), "a" * 64))
    else:
        monkeypatch.setattr(server_migration_timeline, "verify_server_initial_current",
                            lambda *_: False)

    monkeypatch.setattr(mysql_successor, "execute_mysql_new_table_successor",
                        lambda *args, **kwargs: pytest.fail("Untrusted review reached executor"))
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=True, confirm="APPLY", approve="a" * 64,
                           backup=str(tmp_path / "before.sql"), name="Invoices")

    with pytest.raises((ValueError, RuntimeError)):
        cli_server_model_migration.run_server_model_migration(args)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_workflow_dispatches_historically_verified_column_addition(monkeypatch,
                                                                          tmp_path, dialect):
    from aurora.database import (cli_server_model_migration, mysql_column_addition,
                                 postgres_column_addition, server_column_history,
                                 server_model_review)
    from aurora.database.server_model_review import ServerModelProposal

    configure_workflow(monkeypatch, tmp_path, dialect)
    proposal = ServerModelProposal(dialect, ("ALTER TABLE existing_ledgers ADD memo TEXT",),
                                   ({"kind": "add_column", "table": "existing_ledgers",
                                     "column": "memo"},), "a" * 64)
    monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *_: proposal)
    proven = []
    monkeypatch.setattr(server_column_history, "verify_server_column_addition_predecessor",
                        lambda *args: proven.append(args) or ())
    invoked = []
    target = mysql_column_addition if dialect == "mysql" else postgres_column_addition
    function = "execute_mysql_column_addition" if dialect == "mysql" else "execute_postgres_column_addition"
    monkeypatch.setattr(target, function,
                        lambda conn, models, **kwargs: invoked.append(kwargs) or
                        ("operation", Path(kwargs["backup_path"])))
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=True, confirm="APPLY", approve="a" * 64,
                           backup=str(tmp_path / "before.dump"), name="AddMemo")

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert proven and len(invoked) == 1
    assert invoked[0]["alias"] == "accounting"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_workflow_routes_widening_only_after_predecessor_proof(monkeypatch,
                                                                       tmp_path, dialect):
    from aurora.database import (cli_server_model_migration, mysql_column_widening,
                                 postgres_column_widening, server_column_widening,
                                 server_widening_history, server_model_review,
                                 server_migration_timeline)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    configure_workflow(monkeypatch, tmp_path, dialect)
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "accounting", "b" * 64,
                             model_identity_map([ExistingLedger, AddedInvoice]),
                             "c" * 64, "applied"),))
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *_: (_ for _ in ()).throw(ValueError("existing-column drift")))
    proposal = ServerModelProposal(dialect, ("ALTER TABLE existing_ledgers MODIFY name",),
                                   ({"kind": "widen_column", "table": "existing_ledgers",
                                     "column": "name", "from": "varchar(100)",
                                     "to": "varchar(200)"},), "a" * 64)
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *_: proposal)
    proof = []
    monkeypatch.setattr(server_widening_history, "verify_server_widening_predecessor",
                        lambda *args: proof.append(True))
    dispatched = []
    target = mysql_column_widening if dialect == "mysql" else postgres_column_widening
    method = ("execute_mysql_column_widening" if dialect == "mysql"
              else "execute_postgres_column_widening")
    monkeypatch.setattr(target, method, lambda conn, models, **kwargs:
                        dispatched.append(kwargs) or ("op", Path(kwargs["backup_path"])))
    args = SimpleNamespace(config="ignored", database="accounting", title="Expand name",
                           apply=True, confirm="APPLY", approve="a" * 64,
                           backup=str(tmp_path / "before.dump"), name="ExpandName")

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert proof and len(dispatched) == 1
    assert dispatched[0]["alias"] == "accounting"
    assert dispatched[0]["title"] == "Expand name"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_workflow_routes_nullability_only_after_historical_proof(monkeypatch,
                                                                         tmp_path, dialect):
    from aurora.database import (cli_server_model_migration, mysql_nullability_change,
                                 postgres_nullability_change, server_nullability_review,
                                 server_nullability_history, server_column_widening,
                                 server_model_review, server_migration_timeline)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    configure_workflow(monkeypatch, tmp_path, dialect)
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "accounting", "b" * 64,
                             model_identity_map([ExistingLedger, AddedInvoice]),
                             "c" * 64, "applied"),))
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *_: (_ for _ in ()).throw(ValueError("unsupported edit")))
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *_: (_ for _ in ()).throw(ValueError("not widening")))
    proposal = ServerModelProposal(dialect, ("ALTER TABLE existing_ledgers",),
                                   ({"kind": "change_nullability", "table": "existing_ledgers",
                                     "column": "memo", "from": True, "to": False},), "a" * 64)
    monkeypatch.setattr(server_nullability_review, "review_server_nullability_change",
                        lambda *_: proposal)
    proven = []
    monkeypatch.setattr(server_nullability_history, "verify_server_nullability_predecessor",
                        lambda *_: proven.append(True))
    calls = []
    target = mysql_nullability_change if dialect == "mysql" else postgres_nullability_change
    method = ("execute_mysql_nullability_change" if dialect == "mysql"
              else "execute_postgres_nullability_change")
    monkeypatch.setattr(target, method, lambda connection, models, **kwargs:
                        calls.append(kwargs) or ("op", Path(kwargs["backup_path"])))
    args = SimpleNamespace(config="ignored", database="accounting", title="Require memo",
                           apply=True, confirm="APPLY", approve="a" * 64,
                           backup=str(tmp_path / "before.dump"), name="RequireMemo")

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert proven and len(calls) == 1
    assert calls[0]["alias"] == "accounting"
    assert calls[0]["title"] == "Require memo"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_workflow_routes_default_addition_after_original_proof(monkeypatch,
                                                                       tmp_path, dialect):
    from aurora.database import (cli_server_model_migration, mysql_default_addition,
                                 postgres_default_addition, server_default_review,
                                 server_default_history, server_column_widening,
                                 server_nullability_review, server_model_review,
                                 server_migration_timeline)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    configure_workflow(monkeypatch, tmp_path, dialect)
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "accounting", "b" * 64,
                             model_identity_map([ExistingLedger, AddedInvoice]),
                             "c" * 64, "applied"),))
    monkeypatch.setattr(server_model_review, "review_server_model_changes",
                        lambda *_: (_ for _ in ()).throw(ValueError("unsupported edit")))
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *_: (_ for _ in ()).throw(ValueError("not widening")))
    monkeypatch.setattr(server_nullability_review, "review_server_nullability_change",
                        lambda *_: (_ for _ in ()).throw(ValueError("not nullability")))
    proposal = ServerModelProposal(dialect, ("ALTER TABLE existing_ledgers",),
                                   ({"kind": "add_default", "table": "existing_ledgers",
                                     "column": "memo"},), "a" * 64)
    monkeypatch.setattr(server_default_review, "review_server_default_addition",
                        lambda *_: proposal)
    proven = []
    monkeypatch.setattr(server_default_history, "verify_server_default_predecessor",
                        lambda *_: proven.append(True))
    calls = []
    target = mysql_default_addition if dialect == "mysql" else postgres_default_addition
    method = ("execute_mysql_default_addition" if dialect == "mysql"
              else "execute_postgres_default_addition")
    monkeypatch.setattr(target, method, lambda connection, models, **kwargs:
                        calls.append(kwargs) or ("op", Path(kwargs["backup_path"])))
    args = SimpleNamespace(config="ignored", database="accounting", title="Default memo",
                           apply=True, confirm="APPLY", approve="a" * 64,
                           backup=str(tmp_path / "before.dump"), name="DefaultMemo")

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert proven and len(calls) == 1
    assert calls[0]["alias"] == "accounting"
    assert calls[0]["title"] == "Default memo"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize("confirmation", ["yes", "y"])
def test_simple_server_confirmation_preserves_review_and_backup(monkeypatch, tmp_path,
                                                                dialect, confirmation):
    from aurora.database import cli_server_model_migration, mysql_successor, postgres_successor

    configure_workflow(monkeypatch, tmp_path, dialect)
    monkeypatch.setattr(cli_server_model_migration.sys, "stdin",
                        SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input",
                        lambda *_: pytest.fail("Explicit confirmation must not prompt"))
    calls = []

    def execute(connection, models, **kwargs):
        calls.append(kwargs)
        return "operation", Path(kwargs["backup_path"])

    target = mysql_successor if dialect == "mysql" else postgres_successor
    executor = ("execute_mysql_new_table_successor" if dialect == "mysql" else
                "execute_postgres_new_table_successor")
    monkeypatch.setattr(target, executor, execute)
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=False, confirm=confirmation, approve=None, backup=None,
                           name=None)

    assert cli_server_model_migration.run_server_model_migration(args) == 0
    assert len(calls) == 1
    assert calls[0]["approval"] == "a" * 64
    assert calls[0]["title"] == "Untitled"
    assert calls[0]["name"] == "Migration2"
    assert Path(calls[0]["backup_path"]).parent == tmp_path / "backups"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_simple_server_confirmation_refuses_mixed_advanced_flags(monkeypatch, tmp_path,
                                                                 dialect):
    from aurora.database import cli_server_model_migration

    configure_workflow(monkeypatch, tmp_path, dialect)
    args = SimpleNamespace(config="ignored", database="accounting", title=None,
                           apply=False, confirm="yes", approve="a" * 64, backup=None,
                           name=None)

    with pytest.raises(PermissionError, match="Simple confirmation"):
        cli_server_model_migration.run_server_model_migration(args)

    assert not (tmp_path / "backups").exists()
