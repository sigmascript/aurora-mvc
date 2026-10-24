"""Read-only MySQL execution-readiness assessment.

A readiness report is never an approval or an executable migration plan.
"""
from __future__ import annotations

import hashlib
import json

from aurora.database.cli_mysql_policy import review_mysql_schema, review_mysql_plan


def assess_mysql_policy(policy: dict) -> dict:
    """Assess eligibility without converting display messages into write operations."""
    if not isinstance(policy, dict) or policy.get('read_only') is not True or policy.get('executable') is not False:
        raise ValueError('MySQL readiness requires a read-only policy report')
    if policy.get('approval_available') is not False or policy.get('execution_available') is not False:
        raise ValueError('MySQL readiness refuses an approval-capable policy report')
    if policy.get('schema_status') not in ('MATCH', 'DRIFT', 'UNVERIFIED'):
        raise ValueError('Unknown MySQL schema status')
    if policy.get('scope') != 'selected models only' or not isinstance(policy.get('decisions'), list):
        raise ValueError('Invalid MySQL policy scope or decisions')

    decisions = policy['decisions']
    for item in decisions:
        if not isinstance(item, dict) or item.get('decision') not in ('CANDIDATE', 'MANUAL_REVIEW', 'BLOCKED'):
            raise ValueError('Invalid MySQL policy decision')
    # Fingerprint is informational, not an approval token. No DDL may consume it.
    canonical = json.dumps({
        'database_alias': policy.get('database_alias'),
        'schema_status': policy['schema_status'],
        'scope': policy['scope'],
        'decisions': decisions,
    }, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    fingerprint = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    candidates = sum(item['decision'] == 'CANDIDATE' for item in decisions)
    requires_review = policy['schema_status'] != 'MATCH' or bool(decisions) or bool(policy.get('requires_review'))
    return {
        'database_alias': policy.get('database_alias'),
        'schema_status': policy['schema_status'],
        'scope': 'selected models only',
        'read_only': True,
        'executable': False,
        'approval_available': False,
        'execution_available': False,
        'ready_to_execute': False,
        'requires_review': requires_review,
        'candidate_count': candidates,
        'plan_fingerprint_sha256': fingerprint,
        'blocking_reasons': (
            ['No structured, freshly revalidated MySQL DDL operation or approval mechanism exists']
            + (['Schema differences require review'] if requires_review else [])
        ),
        'warning': 'Assessment only; fingerprint is not an approval token. No MySQL migration DDL is generated or executed.',
    }


def assess_mysql_schema(config, alias, *, model_refs=(), models_config='aurora_models.json') -> dict:
    return assess_mysql_policy(review_mysql_schema(config, alias, model_refs=model_refs, models_config=models_config))
