"""Execution-context binding tests; no live database is needed."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import pytest

from aurora.aurorasql.models import Model, column


class ScopedNote(Model):
    __table__ = "scoped_notes"

    id = column(int, primary_key=True)


class ConnectionStub:
    dialect = "sqlite"
    pass


def test_scopes_restore_previous_connection_even_after_failure():
    first = ConnectionStub()
    second = ConnectionStub()
    Model.bind(None)

    with pytest.raises(RuntimeError, match="No database"):
        ScopedNote.legacy_query()

    with Model.connection_scope(first):
        assert ScopedNote.legacy_query()._operations.connection is first
        with pytest.raises(ValueError, match="abort"):
            with Model.connection_scope(second):
                assert ScopedNote.legacy_query()._operations.connection is second
                raise ValueError("abort")
        assert ScopedNote.legacy_query()._operations.connection is first

    with pytest.raises(RuntimeError, match="No database"):
        ScopedNote.legacy_query()


def test_contexts_are_isolated_without_changing_global_binding():
    first = ConnectionStub()
    second = ConnectionStub()
    Model.bind(None)

    def inspect(connection):
        with Model.connection_scope(connection):
            return ScopedNote.legacy_query()._operations.connection

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(inspect, first)
        b = pool.submit(inspect, second)
        assert a.result() is first
        assert b.result() is second

    with pytest.raises(RuntimeError, match="No database"):
        ScopedNote.legacy_query()
