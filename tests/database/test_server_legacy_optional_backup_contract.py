"""Retained server successor executors must not make backup an execution prerequisite."""

from contextlib import contextmanager
from importlib import import_module
from types import SimpleNamespace

import pytest


LEGACY_EXECUTORS = (
    ("mysql_column_addition", "execute_mysql_column_addition"),
    ("mysql_column_widening", "execute_mysql_column_widening"),
    ("mysql_default_addition", "execute_mysql_default_addition"),
    ("mysql_nullability_change", "execute_mysql_nullability_change"),
    ("mysql_successor", "execute_mysql_new_table_successor"),
    ("postgres_column_addition", "execute_postgres_column_addition"),
    ("postgres_column_widening", "execute_postgres_column_widening"),
    ("postgres_default_addition", "execute_postgres_default_addition"),
    ("postgres_nullability_change", "execute_postgres_nullability_change"),
    ("postgres_successor", "execute_postgres_new_table_successor"),
)


@pytest.mark.parametrize("module_name,function_name", LEGACY_EXECUTORS)
def test_legacy_execution_can_reach_lock_without_creating_backup(
        monkeypatch, module_name, function_name):
    module = import_module(f"aurora.database.{module_name}")
    dialect = "mysql" if module_name.startswith("mysql") else "postgresql"

    class ReachedLock(Exception):
        pass

    @contextmanager
    def stop_at_lock(*args, **kwargs):
        raise ReachedLock
        yield

    class Connection:
        _transaction_active = False
        _migration_lock_active = False

        def __init__(self):
            self.dialect = dialect

        def transaction(self):
            return stop_at_lock()

    monkeypatch.setattr(module, "migration_lock", stop_at_lock, raising=False)

    def forbidden_backup(*args, **kwargs):
        pytest.fail("Migration attempted to create a backup automatically")

    if dialect == "mysql":
        from aurora.database import cli_mysql_backup
        monkeypatch.setattr(cli_mysql_backup, "backup_mysql_database", forbidden_backup)
    else:
        from aurora.database import cli_postgres_backup
        monkeypatch.setattr(cli_postgres_backup, "backup_postgres_database", forbidden_backup)

    kwargs = dict(alias="data", identity="verified", parsed=SimpleNamespace(),
                  config_path="config", backup_path=None, review=object(),
                  approval="digest", name="Migration2", title="Safe test")
    if dialect == "mysql":
        kwargs["login_path"] = None

    with pytest.raises(ReachedLock):
        getattr(module, function_name)(Connection(), [], **kwargs)


def test_legacy_server_cli_refuses_retired_migration_backup_destination():
    from aurora.database.cli_server_model_migration import run_server_model_migration

    with pytest.raises(ValueError, match="backup destinations are retired"):
        run_server_model_migration(SimpleNamespace(
            apply=True, confirm="APPLY", approve="digest", backup="backup.sql",
            name="Migration2", format="text", output_dir="migrations"))
