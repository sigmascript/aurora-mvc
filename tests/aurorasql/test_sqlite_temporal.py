"""Regression checks for explicit SQLite temporal parameter adaptation."""

import warnings
from datetime import date, datetime, timezone

from aurora.aurorasql import connect


def test_sqlite_temporal_values_do_not_use_deprecated_global_adapters():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT, day TEXT)")
        timestamp = datetime(2026, 10, 29, 13, 45, 6, 123456)
        day = date(2026, 10, 29)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", DeprecationWarning)
            db.execute_write("INSERT INTO events (at, day) VALUES (:at, :day)",
                             {"at": timestamp, "day": day})
            result = db.fetch_first("SELECT at, day FROM events WHERE at = :at",
                                    {"at": timestamp})
        assert result == {"at": "2026-10-29 13:45:06.123456", "day": "2026-10-29"}
        assert not [w for w in captured if issubclass(w.category, DeprecationWarning)]


def test_sqlite_temporal_parameters_preserve_timezone_offset():
    with connect("sqlite:///:memory:") as db:
        db.execute_write("CREATE TABLE events (at TEXT)")
        stamp = datetime(2026, 10, 29, 13, 45, tzinfo=timezone.utc)
        db.execute_write("INSERT INTO events (at) VALUES (:at)", {"at": stamp})
        assert db.fetch_first("SELECT at FROM events") == {"at": "2026-10-29 13:45:00+00:00"}
