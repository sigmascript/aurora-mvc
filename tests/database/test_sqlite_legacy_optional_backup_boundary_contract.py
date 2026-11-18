"""Retired SQLite execution adapters cannot create migration-time backups."""

from types import SimpleNamespace
from pathlib import Path

import pytest

from aurora.database import cli_explicit_operations as explicit
from aurora.database import cli_model_migration as model_migration


def _args(**changes):
    data = dict(config="aurora_databases.json", database="data", confirm="APPLY",
                approve="reviewed-digest", backup=None)
    data.update(changes)
    return SimpleNamespace(**data)


def test_legacy_sqlite_executor_accepts_reviewed_approval_without_a_backup(monkeypatch, tmp_path):
    class ProbeReached(Exception):
        pass

    def probe(*_args, **_kwargs):
        raise ProbeReached

    monkeypatch.setattr(explicit.sqlite3, "connect", probe)
    with pytest.raises(ProbeReached):
        explicit._apply_sqlite_operations(
            _args(), {"operation": {"kind": "drop_index", "table": "notes", "name": "idx"}},
            tmp_path / "data.sqlite3", "reviewed-digest",
        )


def test_legacy_sqlite_executor_rejects_backup_destination_before_access(tmp_path):
    with pytest.raises(ValueError, match="retired"):
        explicit._apply_sqlite_operations(
            _args(backup=str(tmp_path / "before.sqlite3")),
            {"operation": {"kind": "drop_index", "table": "notes", "name": "idx"}},
            tmp_path / "data.sqlite3", "reviewed-digest",
        )
    assert not (tmp_path / "before.sqlite3").exists()


@pytest.mark.parametrize("confirm,approve", [("yes", "reviewed-digest"), ("APPLY", "wrong")])
def test_legacy_sqlite_executor_still_requires_exact_review(confirm, approve, tmp_path):
    with pytest.raises(PermissionError, match="exact reviewed approval"):
        explicit._apply_sqlite_operations(
            _args(confirm=confirm, approve=approve),
            {"operation": {"kind": "drop_index", "table": "notes", "name": "idx"}},
            tmp_path / "data.sqlite3", "reviewed-digest",
        )


def test_advanced_model_route_does_not_require_backup_but_rejects_old_destination(monkeypatch):
    class ModelDiscoveryReached(Exception):
        pass

    class DatabaseConfig:
        def resolve(self, alias):
            assert alias == "data"
            return "sqlite:///data.sqlite3"

    monkeypatch.setattr(model_migration, "_load_config", lambda _: (DatabaseConfig(), Path(".")))
    monkeypatch.setattr(model_migration, "_select_models", lambda *_: (_ for _ in ()).throw(ModelDiscoveryReached))
    base = dict(config="aurora_databases.json", database="data", dry_run=False, apply=True,
                approve="reviewed-digest", confirm="APPLY", backup=None, check=False,
                explain=False, format="text", review_file=None, changes=None, name=None,
                schema=None, table=None, column=None, approve_review=None,
                approve_sql=None, output_dir="migrations", model=None, title=None)
    with pytest.raises(ModelDiscoveryReached):
        model_migration.run_model_migration(SimpleNamespace(**base))
    base["backup"] = "before.sqlite3"
    with pytest.raises(PermissionError, match="backup destinations are retired"):
        model_migration.run_model_migration(SimpleNamespace(**base))
