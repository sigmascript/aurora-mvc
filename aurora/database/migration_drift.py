"""Phase 6.6.9: advisory schema drift detection; no migration execution.

IMPORTANT: inspect_sqlite uses immutable SQLite mode. Only inspect stable,
quiescent database snapshots; active WAL files can make the result stale.
These checks do not provide an exclusive lock or a race-free execution gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Iterable

from aurora.database.schema import SchemaInfo, inspect_sqlite
from aurora.database.model import Model
from aurora.database.plan import MigrationPlan, plan_schema
from aurora.database.migration_safety import plan_digest


def schema_digest(schema: SchemaInfo) -> str:
    """Stable digest of the supported schema-inspector representation.

    Does not cover triggers, views, CHECK constraints, expression indexes,
    hidden/generated columns, or SQLite metadata omitted by the inspector.
    """
    if not isinstance(schema, SchemaInfo):
        raise TypeError('schema must be a SchemaInfo')
    data = []
    for table in sorted(schema.tables, key=lambda t: t.name):
        data.append({
            'name': table.name,
            'columns': sorted(((c.name, c.type, c.nullable, c.default, c.primary_key_position)
                               for c in table.columns), key=lambda x: x[0]),
            'indexes': sorted(((i.name, i.unique, i.columns, i.origin, i.partial)
                               for i in table.indexes), key=lambda x: x[0]),
            'foreign_keys': sorted(((f.id, f.sequence, f.referenced_table, f.from_column,
                                    f.to_column, f.on_update, f.on_delete)
                                   for f in table.foreign_keys)),
        })
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SchemaBaseline:
    schema_sha256: str
    plan_sha256: str


def capture_baseline(schema: SchemaInfo, plan: MigrationPlan) -> SchemaBaseline:
    if not isinstance(plan, MigrationPlan):
        raise TypeError('plan must be a MigrationPlan')
    return SchemaBaseline(schema_digest(schema), plan_digest(plan))


@dataclass(frozen=True)
class DriftReport:
    expected_schema_sha256: str
    observed_schema_sha256: str
    expected_plan_sha256: str
    observed_plan_sha256: str
    blockers: tuple[str, ...]

    @property
    def matches(self) -> bool:
        return not self.blockers

    @property
    def executable(self) -> bool:
        return False

    def format_text(self) -> str:
        status = 'MATCH' if self.matches else 'DRIFT DETECTED'
        return '\n'.join((f'Schema drift review: {status}',
                          'Migration execution disabled.',
                          *(f'- {blocker}' for blocker in self.blockers)))


def check_schema_drift(baseline: SchemaBaseline, observed: SchemaInfo,
                       models: Iterable[type[Model]], *,
                       include_unmanaged: bool = True) -> DriftReport:
    """Compare a new snapshot to an earlier snapshot and its associated plan.

    The model set and include_unmanaged option must be the same as those used
    when the baseline plan was produced. This function never authorizes writes.
    """
    if not isinstance(baseline, SchemaBaseline):
        raise TypeError('baseline must be a SchemaBaseline')
    observed_sha = schema_digest(observed)
    observed_plan_sha = plan_digest(plan_schema(observed, models,
                                                include_unmanaged=include_unmanaged))
    blockers = []
    if observed_sha != baseline.schema_sha256:
        blockers.append('Supported schema snapshot changed since baseline.')
    if observed_plan_sha != baseline.plan_sha256:
        blockers.append('Migration plan changed since baseline.')
    return DriftReport(baseline.schema_sha256, observed_sha,
                       baseline.plan_sha256, observed_plan_sha, tuple(blockers))


def check_sqlite_drift(path: str | Path, baseline: SchemaBaseline,
                       models: Iterable[type[Model]], *,
                       include_unmanaged: bool = True) -> DriftReport:
    """Read-only inspection for stable, quiescent SQLite databases only."""
    return check_schema_drift(baseline, inspect_sqlite(path), models,
                              include_unmanaged=include_unmanaged)
