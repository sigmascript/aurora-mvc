"""Persisted sessions reject stale, revoked, rotated, and expired tokens."""

from unittest.mock import patch

import pytest

from aurora.security.sessions import SessionConflict, SQLiteSessionStore


def test_stale_writer_and_logout_cannot_restore_session(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    token = store.commit(None, None, {"user": 1})
    _, version = store.load(token)
    store.commit(token, version, {"user": 2})
    with pytest.raises(SessionConflict):
        store.commit(token, version, {"user": 1})
    assert store.load(token)[0] == {"user": 2}

    _, current = store.load(token)
    store.commit(token, current, {}, invalidate=True)
    with pytest.raises(SessionConflict):
        store.commit(token, current, {"user": 1})
    assert store.load(token) is None


def test_rotation_and_expiration_revoke_old_token(tmp_path):
    store = SQLiteSessionStore(tmp_path / "sessions.sqlite3")
    old = store.commit(None, None, {"user": 1})
    _, version = store.load(old)
    new = store.commit(old, version, {"user": 1}, rotate=True)
    assert new != old
    assert store.load(old) is None
    assert store.load(new)[0] == {"user": 1}

    with patch("aurora.security.sessions.time.time", return_value=100):
        expiring = store.commit(None, None, {"user": 1}, lifetime=10)
    with patch("aurora.security.sessions.time.time", return_value=110):
        with pytest.raises(SessionConflict):
            store.commit(expiring, 1, {"user": 2})
