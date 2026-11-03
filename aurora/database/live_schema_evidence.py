"""Conservative live-schema evidence for migration review (read-only).

Model snapshots contain Python-only declarations that cannot be reconstructed
from database catalogs. This module deliberately does not claim fingerprint
parity or grant execution authorization.
"""

from dataclasses import dataclass

from .inspection import inspect_table
from .migration_format import snapshot_fingerprint
from .snapshots import _snapshot_tables


@dataclass(frozen=True)
class LiveSchemaEvidence:
    dialect: str
    reviewed_before_sha256: str
    checked_tables: tuple[str, ...]
    findings: tuple[str, ...]
    execution_allowed: bool = False

    @property
    def structurally_consistent(self):
        return not self.findings


def inspect_reviewed_baseline(connection, before):
    """Check actual table/column presence against a reviewed model snapshot.

    This is structural evidence, NOT a complete schema equality proof:
    indexes, constraints, defaults, types and unexpected tables require a
    separate trustworthy dialect-specific canonicalization before execution.
    """
    tables = _snapshot_tables(before)

    if before["dialect"] != connection.dialect:
        raise ValueError("Snapshot dialect does not match the live connection")

    digest = snapshot_fingerprint(before)
    findings = []

    for name, columns in sorted(tables.items()):
        actual = inspect_table(connection, name)

        if not actual.exists:
            findings.append(f"Missing table: {name}")
            continue

        actual_columns = {column.name for column in actual.columns}
        expected_columns = set(columns)

        for missing in sorted(expected_columns - actual_columns):
            findings.append(f"Missing column: {name}.{missing}")

        for extra in sorted(actual_columns - expected_columns):
            findings.append(f"Unexpected column: {name}.{extra}")

    return LiveSchemaEvidence(connection.dialect, digest, tuple(sorted(tables)), tuple(findings))
