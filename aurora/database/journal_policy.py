"""Pure, read-only migration journal policy checks.

The existing history table describes create-table attempts, not arbitrary
schema migrations. Never reinterpret a legacy entry as a general migration.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class JournalAssessment:
    entry_count: int
    blockers: tuple[str, ...]
    execution_allowed: bool = False


def assess_journal_entries(entries, *, dialect):
    """Validate in-memory journal evidence without changing the database.

    Missing journals are not represented by an empty iterable: callers must
    use list_history(), which raises when the table is missing.
    """

    if dialect not in ("sqlite", "mysql", "postgresql"):
        raise ValueError("Unsupported migration dialect")

    entries = tuple(entries)
    seen_ids = set()
    seen_tables = set()
    blockers = []

    for entry in entries:
        operation_id = getattr(entry, "operation_id", None)
        table = getattr(entry, "table", None)
        status = getattr(entry, "status", None)
        fingerprint = getattr(entry, "fingerprint", None)

        if not isinstance(operation_id, str) or not operation_id:
            blockers.append("Journal entry has invalid operation ID")
        elif operation_id in seen_ids:
            blockers.append(f"Duplicate operation ID: {operation_id}")
        else:
            seen_ids.add(operation_id)

        if not isinstance(table, str) or not table:
            blockers.append("Journal entry has invalid table name")
        elif table in seen_tables:
            blockers.append(f"Repeated table in journal: {table}")
        else:
            seen_tables.add(table)

        if getattr(entry, "dialect", None) != dialect:
            blockers.append(f"Journal dialect mismatch: {operation_id}")

        if not isinstance(fingerprint, str) or len(fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in fingerprint
        ):
            blockers.append(f"Invalid journal fingerprint: {operation_id}")

        if status != "applied":
            blockers.append(f"Unresolved journal status: {operation_id}")

    blockers.append("Legacy create-only journal is not a general migration journal")
    return JournalAssessment(len(entries), tuple(blockers))
