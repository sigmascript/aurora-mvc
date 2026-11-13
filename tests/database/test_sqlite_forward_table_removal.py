"""Guarded, forward-only SQLite table deletion."""
import json
import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.sqlite_forward_history import (
    initialize, migrate_table_removal, read_history, review_table_removal,
)


class Kept(Model):
    __table__ = "kept"
    id = column(int, primary_key=True)


class Retired(Model):
    __table__ = "retired"
    id = column(int, primary_key=True)
    value = column(str)


def setup(tmp_path, populated=True):
    path = tmp_path / "data.sqlite3"
    with sqlite3.connect(path, isolation_level=None) as raw:
        initialize(raw, tmp_path, path, (Kept, Retired), "Initial")
        if populated:
            raw.execute('INSERT INTO "retired" (id,value) VALUES (1,\'important\')')
    return path


def test_populated_table_requires_separate_authorization(tmp_path):
    path = setup(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as raw:
        target, count, witness = review_table_removal(raw, tmp_path, path, (Kept,))
        assert (target, count) == ("retired", 1)
        with pytest.raises(PermissionError, match="double confirmation"):
            migrate_table_removal(raw, tmp_path, path, (Kept,), "Drop", target, count, witness)
        assert raw.execute('SELECT value FROM retired').fetchone() == ("important",)
        assert len(read_history(raw, tmp_path, path)[0]) == 1
        result = migrate_table_removal(raw, tmp_path, path, (Kept,), "Drop", target, count, witness,
                                       acknowledged=True)
        rows, snapshot = read_history(raw, tmp_path, path)
        assert len(rows) == 2
        assert json.loads(rows[-1][-1]) == ["Approved permanent deletion of table retired: 1 existing row(s)"]
        assert result.name == rows[-1][2]
        assert [entry['name'] for entry in snapshot['models']] == ['kept']


def test_empty_table_is_recorded_without_data_loss_warning(tmp_path):
    path = setup(tmp_path, populated=False)
    with sqlite3.connect(path, isolation_level=None) as raw:
        target, count, witness = review_table_removal(raw, tmp_path, path, (Kept,))
        assert count == 0
        migrate_table_removal(raw, tmp_path, path, (Kept,), "Drop", target, count, witness)
        assert read_history(raw, tmp_path, path)[0][-1][-1] is None


def test_stale_data_review_cannot_delete_new_rows(tmp_path):
    path = setup(tmp_path, populated=False)
    with sqlite3.connect(path, isolation_level=None) as raw:
        target, count, witness = review_table_removal(raw, tmp_path, path, (Kept,))
        raw.execute('INSERT INTO "retired" (id,value) VALUES (2,\'new\')')
        with pytest.raises(RuntimeError, match="stale"):
            migrate_table_removal(raw, tmp_path, path, (Kept,), "Drop", target, count, witness)
        assert raw.execute('SELECT value FROM retired').fetchone() == ('new',)
        assert len(read_history(raw, tmp_path, path)[0]) == 1


def test_foreign_key_dependencies_refuse(tmp_path):
    path = setup(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute('CREATE TABLE dependent (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES retired(id))')
        with pytest.raises(RuntimeError, match="Database schema differs"):
            review_table_removal(raw, tmp_path, path, (Kept,))


def test_explicit_index_dependency_refuses(tmp_path):
    path = setup(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as raw:
        raw.execute('CREATE INDEX retired_value ON retired(value)')
        with pytest.raises(RuntimeError, match="Database schema differs"):
            review_table_removal(raw, tmp_path, path, (Kept,))


def test_public_cli_requires_typed_table_for_populated_deletion(tmp_path, monkeypatch):
    import sys
    from aurora import cli
    from aurora.database import cli_model_migration

    path = setup(tmp_path)
    class Config:
        def resolve(self, alias):
            return "sqlite:///data.sqlite3"
    monkeypatch.setattr(cli_model_migration, "_load_config", lambda path: (Config(), tmp_path))
    monkeypatch.setattr(cli_model_migration, "_select_models",
                        lambda args, root: (["kept:Kept"], [Kept]))
    monkeypatch.setattr(sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda _: "wrong")
    assert cli.main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT count(*) FROM retired").fetchone() == (1,)
        assert len(read_history(raw, tmp_path, path)[0]) == 1

    monkeypatch.setattr("builtins.input", lambda _: "retired")
    assert cli.main(["db", "migrate", "--confirm", "yes"]) == 0
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT name FROM sqlite_master WHERE name='retired'").fetchone() is None
        assert len(read_history(raw, tmp_path, path)[0]) == 2


def test_noninteractive_populated_deletion_refused_even_with_confirm_yes(tmp_path, monkeypatch):
    import sys
    from aurora import cli
    from aurora.database import cli_model_migration
    path = setup(tmp_path)

    class Config:
        def resolve(self, alias):
            return "sqlite:///data.sqlite3"
    monkeypatch.setattr(cli_model_migration, "_load_config", lambda path: (Config(), tmp_path))
    monkeypatch.setattr(cli_model_migration, "_select_models",
                        lambda args, root: (["kept:Kept"], [Kept]))
    monkeypatch.setattr(sys, "stdin", type("NoTTY", (), {"isatty": lambda self: False})())
    with pytest.raises(SystemExit) as stopped:
        cli.main(["db", "migrate", "--confirm", "yes"])
    assert stopped.value.code != 0
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT count(*) FROM retired").fetchone() == (1,)
        assert len(read_history(raw, tmp_path, path)[0]) == 1


def test_readonly_check_reports_supported_table_removal(tmp_path, monkeypatch, capsys):
    from aurora import cli
    from aurora.database import cli_plan
    path = setup(tmp_path)

    class Config:
        def resolve(self, alias):
            return "sqlite:///data.sqlite3"
    monkeypatch.setattr(cli_plan, "_load_config", lambda path: (Config(), tmp_path))
    monkeypatch.setattr(cli_plan, "_select_models", lambda args, root: (["kept:Kept"], [Kept]))
    assert cli.main(["db", "check"]) == 2
    output = capsys.readouterr().out
    assert "CHANGES" in output and "TABLE REMOVAL" in output
    with sqlite3.connect(path) as raw:
        assert raw.execute("SELECT count(*) FROM retired").fetchone() == (1,)
        assert len(read_history(raw, tmp_path, path)[0]) == 1


def test_same_row_count_modified_data_refuses(tmp_path):
    path = setup(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as raw:
        target, count, witness = review_table_removal(raw, tmp_path, path, (Kept,))
        raw.execute("UPDATE retired SET value = 'different' WHERE id = 1")
        with pytest.raises(RuntimeError, match="stale"):
            migrate_table_removal(raw, tmp_path, path, (Kept,), "Drop", target, count,
                                  witness, acknowledged=True)
        assert raw.execute('SELECT value FROM retired').fetchone() == ('different',)
        assert len(read_history(raw, tmp_path, path)[0]) == 1
