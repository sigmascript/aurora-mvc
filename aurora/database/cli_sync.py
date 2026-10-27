"""Explicit one-command orchestration of the restricted SQLite migration workflow."""
from __future__ import annotations
import importlib
import re
from pathlib import Path
from aurora.database.cli_inspection import load_cli_database_config, plan_database
from aurora.database.cli_review import create_review
from aurora.database.migration_safety import plan_digest
from aurora.database.migration_operations import review_operations
from aurora.database.sqlite_migration_executor import apply_create_tables
from aurora.database.model import Model


def sync_database(
    *,
    config_path='aurora_databases.json',
    alias='default',
    model_refs=(),
    dry_run=False,
    approve=None,
    name=None,
    output_dir='migrations',
    backup_path=None,
    confirm=None,
):
    """Return (status, digest, tables). Never apply without exact digest and interactive confirmation.

    ``confirm`` is a callable taking the proposed plan text and returning a bool.
    Passing ``approve`` alone never bypasses the interactive confirmation.
    """
    if not model_refs:
        raise ValueError('sync requires explicit --model MODULE:CLASS')
    models = []
    for ref in model_refs:
        module, sep, cls_name = ref.partition(':')
        if not sep or not module or not cls_name.isidentifier():
            raise ValueError('Invalid model reference; expected MODULE:CLASS')
        cls = getattr(importlib.import_module(module), cls_name, None)
        if not isinstance(cls,type) or not issubclass(cls,Model) or cls is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        models.append(cls)
    config, base = load_cli_database_config(config_path)
    # The SQLite sync workflow uses SQLite-only planning and backup semantics.
    # Never interpret a MySQL alias as an executable SQLite migration.
    backend = config.resolve(alias).get_backend_name()
    if backend != "sqlite":
        raise ValueError(
            "aurora db sync supports SQLite execution only; "
            "MySQL migrations require the separate guarded approval workflow"
        )
    plan = plan_database(config, alias, models, base_dir=base)
    digest = plan_digest(plan)
    if plan.is_empty:
        return 'no_changes', digest, ()
    ops = review_operations(plan, models)
    if any(op.kind not in ('missing_table', 'missing_column') or op.classification != 'candidate' for op in ops.operations):
        raise ValueError('sync supports candidate CREATE TABLE and nullable ADD COLUMN operations only')
    # Enforce executor limitations before creating review artifacts.
    by_name = {m.__sql_table__.name: m.__sql_table__ for m in models}
    tables = tuple(
        op.table if op.kind == "missing_table" else f"{op.table}.{op.column}"
        for op in ops.operations
    )
    if len(set(tables))!=len(tables):
        raise ValueError('Duplicate table operation')
    for op in ops.operations:
        table = by_name[op.table]
        if op.kind == 'missing_table':
            if table.foreign_keys or table.indexes or any(col.unique for col in table.columns):
                raise ValueError('Foreign keys, indexes and unique columns are not supported')
        else:
            col = table.c[op.column]
            if (not col.nullable or col.primary_key or col.foreign_keys or col.unique
                    or (col.server_default is not None and not isinstance(col.server_default.arg, str)) or col.computed is not None
                    or col.identity is not None or col.autoincrement is True
                    or any(col in idx.columns for idx in table.indexes)):
                raise ValueError('Only nullable, unindexed columns with optional literal server defaults are supported')
    if dry_run:
        return 'dry_run', digest, tables
    if approve != digest:
        raise ValueError('Explicit --approve must equal the displayed plan SHA-256; run --dry-run first')
    if confirm is None or not confirm(plan.format_text()):
        raise ValueError('Interactive approval declined or unavailable')
    if not name or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}',name):
        raise ValueError('Valid --name is required')
    if not backup_path:
        raise ValueError('--backup is required')
    # Validate both destinations before creating any review artifact. The executor
    # performs its own checks again before opening the database for writes.
    review_path = Path(output_dir).expanduser() / (name + '.json')
    if review_path.is_symlink() or review_path.exists() or not review_path.parent.is_dir():
        raise ValueError('Migration review destination must be a new file in an existing directory')
    backup_target = Path(backup_path).expanduser().absolute()
    if backup_target.is_symlink() or backup_target.exists() or not backup_target.parent.is_dir():
        raise ValueError('Backup destination must be a new file in an existing directory')
    from aurora.database.cli_inspection import _sqlite_path
    database_path = _sqlite_path(config, alias, base).resolve(strict=True)
    if backup_target.resolve() == database_path or review_path.resolve() == database_path:
        raise ValueError('Review and backup must not replace the database')
    if backup_target.resolve() == review_path.resolve():
        raise ValueError('Review and backup destinations must be different')
    review = create_review(review_path, alias=alias, models=list(model_refs), plan=plan)
    manifest, applied = apply_create_tables(
        review, config_path=config_path, approval=digest, backup_path=backup_path
    )
    return 'applied', digest, applied
