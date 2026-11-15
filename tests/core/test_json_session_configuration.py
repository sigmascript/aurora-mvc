"""C2: strict and compatible opt-in standalone session configuration."""
import json
from pathlib import Path

import pytest

from aurora import Aurora
from aurora.security.sessions import SessionSettings


def config(tmp_path, sessions):
    path = tmp_path / 'aurora_config.json'
    path.write_text(json.dumps({'application': {'name': 'Acceptance'}, 'sessions': sessions}), encoding='utf-8')
    return path


def test_config_absent_preserves_stateless_default(tmp_path):
    path = tmp_path / 'aurora_config.json'
    path.write_text('{}')
    assert Aurora.from_project(tmp_path / 'app.py').session_manager is None
    assert not (tmp_path / 'sessions.sqlite3').exists()


def test_explicit_disabled_is_inert(tmp_path):
    app = Aurora.from_config(config(tmp_path, {'enabled': False}))
    assert app.session_manager is None
    assert list(tmp_path.glob('*.sqlite3')) == []


def test_standalone_from_project_uses_project_relative_path(tmp_path, monkeypatch):
    path = config(tmp_path, {'enabled': True, 'path': 'auth.sqlite3', 'secure': False,
                             'cookie_name': 'test_session', 'lifetime': 400,
                             'samesite': 'Strict', 'max_data_bytes': 1024})
    other = tmp_path / 'other'
    other.mkdir()
    monkeypatch.chdir(other)
    app = Aurora.from_project(tmp_path / 'app.py')
    assert app.session_manager is not None
    settings = app.session_manager.settings
    assert settings.database_path == str(tmp_path / 'auth.sqlite3')
    assert settings.cookie_name == 'test_session'
    assert settings.lifetime == 400
    assert settings.samesite == 'Strict'
    assert settings.max_data_bytes == 1024
    assert path.exists() and (tmp_path / 'auth.sqlite3').is_file()
    assert not (other / 'auth.sqlite3').exists()


@pytest.mark.parametrize('settings', [
    True, [], None, {}, {'enabled': 'true'}, {'enabled': True},
    {'enabled': False, 'path': 'bad.sqlite3'},
    {'enabled': True, 'path': '../bad.sqlite3'},
    {'enabled': True, 'path': '/tmp/bad.sqlite3'},
    {'enabled': True, 'path': 'hidden/.sqlite3'},
    {'enabled': True, 'path': '.hidden.sqlite3'},
    {'enabled': True, 'path': 'bad.db'},
    {'enabled': True, 'path': 'good.sqlite3', 'secure': 'false'},
    {'enabled': True, 'path': 'good.sqlite3', 'lifetime': True},
    {'enabled': True, 'path': 'good.sqlite3', 'unexpected': 7},
    {'enabled': True, 'backend': 'database', 'path': 'db.sqlite3'},
    {'enabled': True, 'path': 'good.sqlite3', 'samesite': 'None', 'secure': False},
])
def test_malformed_session_configuration_fails_without_creating_storage(tmp_path, settings):
    with pytest.raises(ValueError):
        Aurora.from_config(config(tmp_path, settings))
    assert not list(tmp_path.glob('*.sqlite3'))


def test_reject_existing_symlink(tmp_path):
    target = tmp_path / 'outside.sqlite3'
    link = tmp_path / 'linked.sqlite3'
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('Symlink not permitted on this platform')
    with pytest.raises(ValueError):
        Aurora.from_config(config(tmp_path, {'enabled': True, 'path': 'linked.sqlite3'}))
    assert not target.exists()


def test_reject_python_configuration_overlap_before_storage_creation(tmp_path):
    path = config(tmp_path, {'enabled': True, 'path': 'good.sqlite3'})
    for overrides in ({'sessions': False}, {'sessions': True},
                      {'session_settings': SessionSettings(str(tmp_path / 'other.sqlite3'))}):
        with pytest.raises(ValueError, match='both JSON and Python'):
            Aurora.from_config(path, **overrides)
    assert not (tmp_path / 'good.sqlite3').exists()


def test_existing_python_settings_still_work(tmp_path):
    path = tmp_path / 'aurora_config.json'
    path.write_text('{}')
    settings = SessionSettings(str(tmp_path / 'python.sqlite3'), secure=False)
    app = Aurora.from_config(path, sessions=settings)
    assert app.session_manager is not None
    assert (tmp_path / 'python.sqlite3').exists()
