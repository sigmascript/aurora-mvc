"""Consolidated, read-only MySQL migration safety review.

Each inspection is a separate observation. Advisory locks are not schema locks;
this report never establishes execution readiness or approval.
"""
from __future__ import annotations

from dataclasses import dataclass

from aurora.database.mysql_live_preflight import inspect_mysql_preflight
from aurora.database.mysql_locked_preflight import revalidate_mysql_preflight_locked
from aurora.database.mysql_deep_schema_review import inspect_mysql_deep_schema


@dataclass(frozen=True)
class MySQLIntegratedReview:
    database_alias: str
    schema_name: str
    snapshot_fingerprint: str
    candidate_tables: tuple[str, ...]
    findings: tuple[str, ...]
    unverified: tuple[str, ...]
    lock_revalidated: bool
    read_only: bool = True
    executable: bool = False
    approval_available: bool = False

    @property
    def status(self) -> str:
        return 'REVIEW_REQUIRED' if self.findings else 'UNVERIFIED'


def review_mysql_migration_safety(config, alias, *, model_refs=(),
                                  models_config='aurora_models.json') -> MySQLIntegratedReview:
    """Inspect selected models and recheck missing-table candidates under locks.

    A failure anywhere aborts the review. A successful result remains advisory:
    full table definitions, external writers and atomicity are not established.
    """
    snapshot, candidates = inspect_mysql_preflight(
        config, alias, model_refs=model_refs, models_config=models_config,
    )
    deep = inspect_mysql_deep_schema(
        config, alias, model_refs=model_refs, models_config=models_config,
    )
    if deep.database_alias != snapshot.database_alias or deep.schema_name != snapshot.schema_name:
        raise RuntimeError('MySQL inspection target changed during review')
    if deep.read_only is not True or deep.executable is not False:
        raise RuntimeError('MySQL deep review is not a trusted read-only result')

    for candidate in candidates:
        if candidate.database_alias != alias or candidate.schema_name != snapshot.schema_name:
            raise RuntimeError('MySQL candidate target mismatch')
        confirmed = revalidate_mysql_preflight_locked(
            config, reviewed=snapshot, candidate=candidate,
        )
        if confirmed != snapshot:
            raise RuntimeError('MySQL schema changed during locked revalidation')

    # An existing-table review cannot establish full compatibility: deep review
    # intentionally records unknown defaults, constraints and table options.
    unverified = tuple(deep.unverified) + (
        'Inspections use separate connections; no atomic schema snapshot is guaranteed',
        'Advisory locks do not prevent changes by non-cooperating clients',
        'No DDL execution or approval is available',
    )
    return MySQLIntegratedReview(
        database_alias=alias,
        schema_name=snapshot.schema_name,
        snapshot_fingerprint=snapshot.fingerprint(),
        candidate_tables=tuple(c.table_name for c in candidates),
        findings=tuple(deep.findings),
        unverified=unverified,
        lock_revalidated=bool(candidates),
    )
