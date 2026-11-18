"""Retired MySQL migration login-path option stays unavailable."""

import pytest

from aurora import cli
from aurora.database import cli_model_migration


def test_model_migrate_uses_no_public_login_path(monkeypatch):
    seen = {}
    def migration(args):
        seen.update(database=args.database, login_path=args.login_path,
                    title=args.title, changes=args.changes)
        return 0
    monkeypatch.setattr(cli_model_migration, 'run_model_migration', migration)
    assert cli.main(['db', 'migrate', '--database', 'data_mysql',
                     '--title', 'Guarded removal']) == 0
    assert seen == {'database': 'data_mysql', 'login_path': None,
                    'title': 'Guarded removal', 'changes': None}


@pytest.mark.parametrize('other', [[], ['-c', 'changes.json'],
                                    ['--reconcile-interrupted', 'mysql_fk_child.parent_id']])
def test_retired_login_path_rejected_for_migrations(other):
    with pytest.raises(SystemExit) as exc:
        cli.main(['db', 'migrate', '--database', 'data_mysql',
                  '--login-path', 'aurora_mvc_test', *other])
    assert exc.value.code == 2
