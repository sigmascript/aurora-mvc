"""Native timeline state transitions require backend coordination and exact state."""

import pytest

from aurora.database import migration_timeline as legacy
from aurora.database import migration_timeline as timeline


class RecordingConnection:
    def __init__(self, dialect="mysql", *, lock=False, transaction=False, affected=1):
        self.dialect = dialect
        self._migration_lock_active = lock
        self._transaction_active = transaction
        self.affected = affected
        self.writes = []

    def execute_write(self, statement, parameters):
        self.writes.append((statement, parameters))
        return self.affected


@pytest.mark.parametrize(
    "name",
    [
        "mark_server_initial_uncertain",
        "complete_server_initial",
        "complete_server_successor",
        "mark_server_successor_uncertain",
    ],
)
def test_legacy_timeline_entrypoints_are_identical(name):
    assert getattr(legacy, name) is getattr(timeline, name)


def test_mysql_uncertain_initial_requires_lock_and_exact_prepared_row():
    unlocked = RecordingConnection()
    with pytest.raises(RuntimeError, match="held MySQL migration lock"):
        timeline.mark_server_initial_uncertain(unlocked, "default")
    assert unlocked.writes == []

    missing = RecordingConnection(lock=True, affected=0)
    with pytest.raises(RuntimeError, match="cannot be quarantined"):
        timeline.mark_server_initial_uncertain(missing, "default")
    assert len(missing.writes) == 1
    assert missing.writes[0][1]["before"] == "prepared"
    assert missing.writes[0][1]["state"] == "uncertain"


def test_mysql_successor_uncertainty_requires_lock_and_prepared_match():
    unlocked = RecordingConnection()
    with pytest.raises(RuntimeError, match="held MySQL migration lock"):
        timeline.mark_server_successor_uncertain(unlocked, 2, "change", "default")
    assert unlocked.writes == []
    locked = RecordingConnection(lock=True)
    timeline.mark_server_successor_uncertain(locked, 2, "change", "default")
    assert locked.writes[0][1]["before"] == "prepared"
    assert locked.writes[0][1]["state"] == "uncertain"


def test_postgres_initial_completion_requires_transaction():
    connection = RecordingConnection("postgresql")
    digest = "a" * 64
    with pytest.raises(RuntimeError, match="active transaction"):
        timeline.complete_server_initial(connection, "default", digest)
    assert connection.writes == []
    connection._transaction_active = True
    timeline.complete_server_initial(connection, "default", digest)
    assert connection.writes[0][1]["before"] == "prepared"
    assert connection.writes[0][1]["schema"] == digest


def test_successor_completion_rejects_invalid_digest_without_writes():
    connection = RecordingConnection(lock=True)
    with pytest.raises(ValueError, match="schema evidence"):
        timeline.complete_server_successor(connection, 2, "change", "default", "bad")
    assert connection.writes == []
