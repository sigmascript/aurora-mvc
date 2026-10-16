"""Non-executable named-database review artifacts; no schema mutations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from aurora.database.migration_safety import plan_digest, review_migration
from aurora.database.migration_readiness import assess_readiness


def validate_plan(plan):
    """Return conservative advisory reports, never execution authorization."""
    return review_migration(plan), assess_readiness(plan)


def create_review(path: str | Path, *, alias: str, models: list[str], plan):
    """Write an exclusive, non-executable JSON review; refuse overwrites."""
    target = Path(path).expanduser().absolute()
    if (
        target.suffix != '.json'
        or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.json', target.name)
    ):
        raise ValueError('Review filename must be a simple .json filename')
    if target.is_symlink() or not target.parent.is_dir():
        raise ValueError('Review destination must be an existing directory and not a symlink')
    document = {
        'format': 'aurora-migration-review-v1', 'executable': False,
        'database_alias': alias, 'models': list(models), 'plan_sha256': plan_digest(plan),
        'steps': [dict(kind=s.kind, table=s.table, column=s.column, actual=s.actual,
                       expected=s.expected, risk=s.risk, recommendation=s.recommendation)
                  for s in plan.steps],
    }
    payload = json.dumps(document, indent=2, ensure_ascii=False) + '\n'
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(payload)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def show_review(path: str | Path):
    from aurora.database.plan import MigrationPlan, MigrationStep
    from aurora.database.migration_safety import plan_digest
    location = Path(path).expanduser()
    if location.is_symlink() or not location.is_file():
        raise ValueError('Review file must be an existing regular file')
    data = json.loads(location.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or set(data) != {'format','executable','database_alias','models','plan_sha256','steps'}:
        raise ValueError('Invalid review format')
    if data['format'] != 'aurora-migration-review-v1' or data['executable'] is not False:
        raise ValueError('Unsupported or executable review file')
    if not isinstance(data['database_alias'], str) or not isinstance(data['models'], list) or not all(isinstance(x,str) for x in data['models']):
        raise ValueError('Invalid review metadata')
    if not isinstance(data['steps'], list):
        raise ValueError('Invalid review steps')
    keys = {'kind','table','column','actual','expected','risk','recommendation'}
    steps = []
    for item in data['steps']:
        if not isinstance(item, dict) or set(item) != keys or any(not isinstance(item[k],str) for k in ('kind','table','risk','recommendation')) or any(item[k] is not None and not isinstance(item[k],str) for k in ('column','actual','expected')):
            raise ValueError('Invalid review step')
        steps.append(MigrationStep(**item))
    plan = MigrationPlan(tuple(steps))
    if plan_digest(plan) != data['plan_sha256']:
        raise ValueError('Review digest mismatch; file may have changed')
    return data, plan
