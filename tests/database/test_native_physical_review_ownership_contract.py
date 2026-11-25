"""Native physical-history verification is read-only and owned by review."""

import pytest

from aurora.database import migration_review


@pytest.mark.parametrize(
    "function_name",
    ["verify_server_initial_current", "verify_server_successor_current"],
)
def test_unavailable_native_timeline_fails_closed_without_writes(monkeypatch, function_name):
    monkeypatch.setattr(migration_review, "inspect_server_timeline", lambda connection, alias: None)

    class ReadOnlyConnection:
        dialect = "mysql"

        def execute_write(self, *args, **kwargs):
            pytest.fail("Physical history review must not write to the database")

    verifier = getattr(migration_review, function_name)
    assert verifier(ReadOnlyConnection(), [], "default") is False
