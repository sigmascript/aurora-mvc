"""Safe, actionable database configuration failures via the public CLI."""
import json
from types import SimpleNamespace

import pytest

from aurora.cli import _database_failure, main
from aurora.database.configuration import DatabaseConfiguration


@pytest.mark.parametrize('entry,expected', [
    ({'engine':'mysql','database':'demo','user':'old'}, "'username'; remove any unknown fields"),
    ({'engine':'postgresql','database':'demo'}, "'username'; remove any unknown fields"),
    ({'engine':'mysql','database':'demo','username':'app','pwssord_invalid':'secret-value'}, "remove any unknown fields"),
    ({'engine':'mysql','database':'demo','username':'app','password':'a','password_env':'X'}, "either 'password'"),
    ({'engine':'mysql','database':'demo','username':'app','port':0}, "'port'"),
    ({'engine':'sqlite','database':'data.sqlite3','username':'bad'}, "SQLite accepts only"),
    ({'engine':'mysql','database':'demo','username':'app','host':'/secret'}, "'host'"),
    ({'url':'sqlite:///data.sqlite3','engine':'sqlite'}, "either 'url'/'url_env'"),
    ({'url':'unrecognized://secret@bad/demo'}, 'supported SQLite'),
])
def test_rejected_config_explains_specific_correction_without_echoing_secrets(entry, expected):
    with pytest.raises(ValueError) as caught:
        DatabaseConfiguration({'default':entry})
    message=_database_failure(caught.value, SimpleNamespace(config='aurora_databases.json'), 'check')
    assert expected in message
    assert 'ValueError' not in message
    assert 'No database changes were made' in message
    assert 'unrecognized://secret' not in message


def test_actual_cli_user_key_reports_username_without_creating_database(tmp_path,monkeypatch,capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'aurora_databases.json').write_text(json.dumps({'databases':{'default':{
        'engine':'mysql','database':'test','user':'testuser','password':'secret-value'
    }}}))
    with pytest.raises(SystemExit) as caught:
        main(['db','check'])
    assert caught.value.code==1
    output=capsys.readouterr().err
    assert "'username'; remove any unknown fields" in output
    assert 'secret-value' not in output
    assert not (tmp_path/'test').exists()


def test_actual_cli_bad_json_explains_config_file(tmp_path,monkeypatch,capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'aurora_databases.json').write_text('{invalid')
    with pytest.raises(SystemExit) as caught:
        main(['db','check'])
    assert caught.value.code==1
    assert 'valid JSON' in capsys.readouterr().err


def test_missing_default_and_duplicated_destinations_are_specific():
    arguments=SimpleNamespace(config='aurora_databases.json')
    with pytest.raises(ValueError) as missing:
        DatabaseConfiguration({'named':{'engine':'sqlite','database':'one.db'}})
    assert "Add a 'default'" in _database_failure(missing.value,arguments,'check')
    with pytest.raises(ValueError) as duplicated:
        DatabaseConfiguration({'default':{'engine':'sqlite','database':'same.db'},
                               'other':{'engine':'sqlite','database':'same.db'}})
    message=_database_failure(duplicated.value,arguments,'check')
    assert 'same database' in message and 'ValueError' not in message


def test_unknown_external_exception_remains_redacted():
    exc=ValueError('sensitive password=secret and full host')
    message=_database_failure(exc,SimpleNamespace(config='aurora_databases.json'),'check')
    assert 'secret' not in message
    assert 'refused (ValueError)' in message
