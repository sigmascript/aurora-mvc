"""Single-column deletion is guarded by real data and schema evidence."""
import json
import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.sqlite_forward_history import (
    initialize, read_history, review_column_removal, migrate_column_removal,
)


class Before(Model):
    __table__ = "customers"
    id = column(int, primary_key=True)
    email = column(str, nullable=True)


class After(Model):
    __table__ = "customers"
    id = column(int, primary_key=True)


def make_db(tmp_path, value="stored"):
    path = tmp_path / "data.sqlite3"
    with sqlite3.connect(path, isolation_level=None) as db:
        initialize(db, tmp_path, path, (Before,), "Initial")
        db.execute('INSERT INTO customers (id,email) VALUES (1,?)', (value,))
    return path


def test_populated_column_needs_authorization_and_records_warning(tmp_path):
    path = make_db(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as db:
        proposal = review_column_removal(db, tmp_path, path, (After,))
        assert proposal[:3] == ("customers", "email", 1)
        with pytest.raises(PermissionError, match="double confirmation"):
            migrate_column_removal(db, tmp_path, path, (After,), "Remove email", *proposal)
        assert db.execute('SELECT email FROM customers').fetchone() == ("stored",)
        migrate_column_removal(db, tmp_path, path, (After,), "Remove email", *proposal,
                               acknowledged=True)
        rows, snapshot = read_history(db, tmp_path, path)
        assert len(rows) == 2
        assert json.loads(rows[-1][-1]) == [
            'Approved permanent deletion of customers.email: 1 existing non-NULL value(s)'
        ]
        assert db.execute('SELECT id FROM customers').fetchall() == [(1,)]
        assert [x['name'] for x in snapshot['models']] == ['customers']


def test_null_column_needs_no_loss_warning(tmp_path):
    path = make_db(tmp_path, None)
    with sqlite3.connect(path, isolation_level=None) as db:
        proposal = review_column_removal(db, tmp_path, path, (After,))
        assert proposal[2] == 0
        migrate_column_removal(db, tmp_path, path, (After,), 'Remove unused', *proposal)
        assert read_history(db, tmp_path, path)[0][-1][-1] is None
        assert db.execute('SELECT id FROM customers').fetchall() == [(1,)]


def test_changed_data_refuses_without_schema_mutation(tmp_path):
    path = make_db(tmp_path, None)
    with sqlite3.connect(path, isolation_level=None) as db:
        proposal = review_column_removal(db, tmp_path, path, (After,))
        db.execute("UPDATE customers SET email = 'new'")
        with pytest.raises(RuntimeError, match='stale'):
            migrate_column_removal(db, tmp_path, path, (After,), 'Remove', *proposal)
        assert db.execute('SELECT email FROM customers').fetchone() == ('new',)
        assert len(read_history(db, tmp_path, path)[0]) == 1


def test_index_dependencies_refused(tmp_path):
    path = make_db(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute('CREATE INDEX customers_email ON customers(email)')
        with pytest.raises(RuntimeError, match='schema differs'):
            review_column_removal(db, tmp_path, path, (After,))


def test_cli_requires_exact_column_identity(tmp_path, monkeypatch):
    import sys
    from aurora import cli
    from aurora.database import cli_model_migration

    path = make_db(tmp_path)
    class Config:
        def resolve(self, alias):
            return 'sqlite:///data.sqlite3'
    monkeypatch.setattr(cli_model_migration, '_load_config', lambda _: (Config(), tmp_path))
    monkeypatch.setattr(cli_model_migration, '_select_models',
                        lambda args, root: (['customer:After'], [After]))
    monkeypatch.setattr(sys, 'stdin', type('TTY', (), {'isatty': lambda self: True})())
    monkeypatch.setattr('builtins.input', lambda _: 'wrong')
    assert cli.main(['db', 'migrate', '--confirm', 'yes']) == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT email FROM customers').fetchone() == ('stored',)
    monkeypatch.setattr('builtins.input', lambda _: 'customers.email')
    assert cli.main(['db', 'migrate', '--confirm', 'yes']) == 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id FROM customers').fetchone() == (1,)
        assert len(read_history(db, tmp_path, path)[0]) == 2


def test_cli_noninteractive_refuses_populated_column(tmp_path, monkeypatch):
    import sys
    from aurora import cli
    from aurora.database import cli_model_migration
    path = make_db(tmp_path)

    class Config:
        def resolve(self, alias):
            return 'sqlite:///data.sqlite3'

    monkeypatch.setattr(cli_model_migration, '_load_config', lambda _: (Config(), tmp_path))
    monkeypatch.setattr(cli_model_migration, '_select_models',
                        lambda args, root: (['customer:After'], [After]))
    monkeypatch.setattr(sys, 'stdin', type('NoTTY', (), {'isatty': lambda self: False})())
    with pytest.raises(SystemExit) as exc:
        cli.main(['db', 'migrate', '--confirm', 'yes'])
    assert exc.value.code != 0
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT email FROM customers').fetchone() == ('stored',)
        assert len(read_history(db, tmp_path, path)[0]) == 1


def test_readonly_check_recognizes_supported_column_removal(tmp_path, monkeypatch, capsys):
    from aurora import cli
    from aurora.database import cli_plan
    path = make_db(tmp_path)

    class Config:
        def resolve(self, alias):
            return 'sqlite:///data.sqlite3'

    monkeypatch.setattr(cli_plan, '_load_config', lambda _: (Config(), tmp_path))
    monkeypatch.setattr(cli_plan, '_select_models',
                        lambda args, root: (['customer:After'], [After]))
    assert cli.main(['db', 'check']) == 2
    assert '[COLUMN REMOVAL]' in capsys.readouterr().out
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT email FROM customers').fetchone() == ('stored',)
        assert len(read_history(db, tmp_path, path)[0]) == 1


def test_same_nonnull_count_modified_data_refuses(tmp_path):
    path = make_db(tmp_path)
    with sqlite3.connect(path, isolation_level=None) as db:
        proposal = review_column_removal(db, tmp_path, path, (After,))
        db.execute("UPDATE customers SET email = 'changed' WHERE id = 1")
        with pytest.raises(RuntimeError, match='stale'):
            migrate_column_removal(db, tmp_path, path, (After,), 'Remove', *proposal,
                                   acknowledged=True)
        assert db.execute('SELECT email FROM customers').fetchone() == ('changed',)
        assert len(read_history(db, tmp_path, path)[0]) == 1
