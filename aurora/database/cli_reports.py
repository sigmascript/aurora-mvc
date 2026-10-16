"""Deterministic read-only migration reports for explicit model selections."""
from __future__ import annotations

import importlib
from aurora.database.cli_inspection import load_cli_database_config, plan_database
from aurora.database.migration_operations import review_operations
from aurora.database.migration_safety import plan_digest
from aurora.database.model import Model


def report_for_plan(alias, refs, plan, models):
    operations = review_operations(plan, models).operations
    changes = []
    for op in operations:
        candidate = op.classification == 'candidate'
        changes.append({
            'kind': op.kind, 'table': op.table, 'column': op.column,
            'classification': 'CANDIDATE' if candidate else 'BLOCKED',
            'reason': op.reason,
            'next_step': ('Review DDL, constraints, live schema and approval requirements before applying.'
                          if candidate else 'Manual schema review required; automatic sync must not apply this difference.'),
        })
    return validate_report_contract({
        'format_version': 1, 'database': alias, 'selected_models': list(refs),
        'plan_sha256': plan_digest(plan), 'schema_status': 'MATCH' if not changes else 'DRIFT',
        'has_blockers': any(c['classification'] == 'BLOCKED' for c in changes),
        'changes': changes, 'read_only': True,
        'notice': 'CANDIDATE is advisory, not execution approval; no schema changes performed.',
    })


def report_for_refs(*, config_path, alias, model_refs):
    models = []
    for ref in model_refs:
        module, separator, name = ref.partition(':')
        if not separator or not module or not name.isidentifier():
            raise ValueError('Invalid model reference; expected MODULE:CLASS')
        cls = getattr(importlib.import_module(module), name, None)
        if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        models.append(cls)
    config, base_dir = load_cli_database_config(config_path)
    plan = plan_database(config, alias, models, base_dir=base_dir)
    return report_for_plan(alias, model_refs, plan, models)


def validate_report_contract(report):
    """Validate the v1 report contract without importing models or touching databases.

    Raises ValueError for malformed reports; returns the input on success.
    """
    if (
        not isinstance(report, dict)
        or type(report.get('format_version')) is not int
        or report['format_version'] != 1
    ):
        raise ValueError('Unsupported migration report format version')
    if not isinstance(report.get('database'), str) or not report['database']:
        raise ValueError('Invalid database alias')
    if (
        not isinstance(report.get('selected_models'), list)
        or not all(isinstance(x, str) for x in report['selected_models'])
    ):
        raise ValueError('Invalid selected_models')
    digest = report.get('plan_sha256')
    if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
        raise ValueError('Invalid plan digest')
    changes = report.get('changes')
    if not isinstance(changes, list):
        raise ValueError('Invalid changes')
    for change in changes:
        if not isinstance(change, dict) or change.get('classification') not in ('CANDIDATE', 'BLOCKED'):
            raise ValueError('Invalid change classification')
        if not all(isinstance(change.get(key), str) for key in ('kind', 'table', 'reason', 'next_step')):
            raise ValueError('Invalid change description')
        if change.get('column') is not None and not isinstance(change['column'], str):
            raise ValueError('Invalid change column')
    if report.get('schema_status') != ('DRIFT' if changes else 'MATCH'):
        raise ValueError('Inconsistent schema status')
    if type(report.get('has_blockers')) is not bool or report['has_blockers'] != any(c['classification'] == 'BLOCKED' for c in changes):
        raise ValueError('Inconsistent blocker status')
    if report.get('read_only') is not True or not isinstance(report.get('notice'), str):
        raise ValueError('Invalid read-only report metadata')
    return report
