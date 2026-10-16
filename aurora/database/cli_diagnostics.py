"""Read-only, explanatory SQLite migration diagnostics.

These classifications are advisory and never grant migration approval.
"""
from __future__ import annotations

from aurora.database.migration_operations import review_operations


def explain_plan(plan, models) -> str:
    """Explain each difference without changing the plan, digest, or database."""
    review = review_operations(plan, models)
    if not review.operations:
        return 'Migration diagnostics: no changes required (selected models only).'
    lines = ['Migration diagnostics (ADVISORY; no approval implied):']
    for item in review.operations:
        location = item.table + ('.' + item.column if item.column else '')
        label = 'CANDIDATE' if item.classification == 'candidate' else 'BLOCKED'
        lines.append(f'  [{label}] {item.kind}: {location}')
        lines.append(f'    Reason: {item.reason}')
        if label == 'CANDIDATE':
            lines.append('    Next: Review DDL, constraints, live schema and approval requirements before applying.')
        else:
            lines.append('    Next: Manual schema review required; automatic sync must not apply this difference.')
    lines.append('Candidate is not an execution guarantee. No schema changes performed.')
    return '\n'.join(lines)
