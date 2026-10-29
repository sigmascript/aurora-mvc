"""Read-only MySQL planning from conservative model/schema comparisons.

This module produces advisory classifications, never SQL or migration approvals.
"""
from __future__ import annotations

from dataclasses import dataclass

from aurora.database.cli_mysql_compat import check_mysql_compatibility


@dataclass(frozen=True)
class MySQLPlanItem:
    category: str
    policy: str
    detail: str

    def as_dict(self):
        return {"category": self.category, "policy": self.policy, "detail": self.detail}


def _classify(detail: str) -> tuple[str, str]:
    if detail.startswith('Missing table: '):
        return 'MISSING_TABLE', 'REVIEW_REQUIRED'
    if ': missing column ' in detail:
        return 'MISSING_COLUMN', 'REVIEW_REQUIRED'
    if ': unexpected column ' in detail:
        return 'UNEXPECTED_COLUMN', 'BLOCKED'
    if ': type mismatch ' in detail:
        return 'TYPE_CHANGE', 'MANUAL_REVIEW'
    if ': nullability differs' in detail:
        return 'NULLABILITY_CHANGE', 'MANUAL_REVIEW'
    if ': primary key differs' in detail:
        return 'PRIMARY_KEY_CHANGE', 'BLOCKED'
    if ': index definitions differ' in detail:
        return 'INDEX_CHANGE', 'MANUAL_REVIEW'
    if ': foreign key definitions differ' in detail:
        return 'FOREIGN_KEY_CHANGE', 'MANUAL_REVIEW'
    return 'OTHER_DIFFERENCE', 'MANUAL_REVIEW'


def plan_mysql_schema(config, alias, *, model_refs=(), models_config='aurora_models.json'):
    """Return an advisory report. No DDL, SQL generation, or approval tokens."""
    status, drift, unverified = check_mysql_compatibility(
        config, alias, model_refs=model_refs, models_config=models_config,
    )
    items = [MySQLPlanItem(*_classify(message), message) for message in drift]
    items.extend(MySQLPlanItem('UNVERIFIED', 'UNVERIFIED', message) for message in unverified)
    return {
        'database_alias': alias,
        'schema_status': status,
        'read_only': True,
        'executable': False,
        'scope': 'selected models only',
        'items': [item.as_dict() for item in items],
    }
