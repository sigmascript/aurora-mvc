"""Conservative, read-only MySQL schema compatibility for selected Aurora models."""
from __future__ import annotations

import importlib

from sqlalchemy import create_engine, inspect as sa_inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.sql.sqltypes import Integer, String, Text, Boolean, DateTime, Date, Float, Numeric

from aurora.database.cli_model_manifest import select_model_refs
from aurora.database.model import Model


def _type_signature(type_):
    """Normalize only representations whose equivalence is well established."""
    name = type(type_).__name__.lower()
    if isinstance(type_, Boolean) or name in ('tinyint',) and getattr(type_, 'display_width', None) == 1:
        return ('bool',)
    if isinstance(type_, Integer):
        if name in ('integer', 'int'):
            return ('int',)
        return (name,) if name in ('bigint', 'smallint') else None
    if isinstance(type_, String):
        length = getattr(type_, 'length', None)
        if name in ('string', 'varchar') and length is not None:
            return ('varchar', length)
        return None
    if isinstance(type_, Text):
        return ('text',) if name == 'text' else None
    if isinstance(type_, DateTime):
        return ('datetime',) if name == 'datetime' else None
    if isinstance(type_, Date):
        return ('date',) if name == 'date' else None
    if isinstance(type_, Float):
        return None  # MySQL FLOAT precision and SQLAlchemy Float differ.
    if isinstance(type_, Numeric):
        return ('decimal', type_.precision, type_.scale) if type_.precision is not None else None
    return None


def _compare_table(inspector, table):
    drift = []
    unknown = []
    name = table.name
    actual_columns = {item['name']: item for item in inspector.get_columns(name)}
    expected_columns = {item.name: item for item in table.columns}
    for col in sorted(set(expected_columns) - set(actual_columns)):
        drift.append(f'{name}: missing column {col}')
    for col in sorted(set(actual_columns) - set(expected_columns)):
        drift.append(f'{name}: unexpected column {col}')
    for col in sorted(set(expected_columns) & set(actual_columns)):
        expected = expected_columns[col]
        actual = actual_columns[col]
        exp_type = _type_signature(expected.type)
        act_type = _type_signature(actual['type'])
        if exp_type is None or act_type is None:
            unknown.append(f'{name}.{col}: unsupported type comparison ({expected.type} vs {actual["type"]})')
        elif exp_type != act_type:
            drift.append(f'{name}.{col}: type mismatch ({exp_type} vs {act_type})')
        if bool(expected.nullable) != bool(actual['nullable']):
            drift.append(f'{name}.{col}: nullability differs')
    actual_pk = tuple(inspector.get_pk_constraint(name).get('constrained_columns') or ())
    expected_pk = tuple(col.name for col in table.primary_key.columns)
    if actual_pk != expected_pk:
        drift.append(f'{name}: primary key differs ({expected_pk} vs {actual_pk})')

    actual_indexes = {
        (tuple(item.get('column_names') or ()), bool(item.get('unique', False)))
        for item in inspector.get_indexes(name)
        if not item.get('duplicates_constraint')
    }
    expected_indexes = {
        (tuple(col.name for col in index.columns), bool(index.unique))
        for index in table.indexes
    }
    # MySQL reports UNIQUE constraints as indexes; model UniqueConstraints
    # are included to avoid false drift.
    from sqlalchemy import UniqueConstraint
    expected_indexes |= {
        (tuple(col.name for col in constraint.columns), True)
        for constraint in table.constraints if isinstance(constraint, UniqueConstraint)
    }
    if actual_indexes != expected_indexes:
        drift.append(f'{name}: index definitions differ')

    actual_fks = {
        (tuple(item.get('constrained_columns') or ()), item.get('referred_table'),
         tuple(item.get('referred_columns') or ()))
        for item in inspector.get_foreign_keys(name)
    }
    expected_fks = {
        (tuple(element.parent.name for element in fk.elements),
         next(iter(fk.elements)).column.table.name,
         tuple(element.column.name for element in fk.elements))
        for fk in table.foreign_key_constraints
    }
    if actual_fks != expected_fks:
        drift.append(f'{name}: foreign key definitions differ')
    # Defaults, check constraints, collations, and FK actions are not
    # fully checked. Never call a partial structural match a full MATCH.
    if any(col.server_default is not None for col in table.columns):
        unknown.append(f'{name}: server defaults are not verified')
    if any(fk.ondelete or fk.onupdate for fk in table.foreign_key_constraints):
        unknown.append(f'{name}: foreign key actions are not verified')
    return drift, unknown


def check_mysql_compatibility(config, alias, *, model_refs=(), models_config='aurora_models.json'):
    """Return (status, drift, unverified); never execute schema-changing SQL."""
    url = config.resolve(alias)
    if url.get_backend_name() != 'mysql':
        raise ValueError('MySQL schema compatibility requires a MySQL database alias')
    refs = select_model_refs(explicit=model_refs, database=alias, manifest=models_config)
    tables = {}
    for ref in refs:
        module, _, cls_name = ref.partition(':')
        model = getattr(importlib.import_module(module), cls_name, None)
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise ValueError(f'Not an Aurora model: {ref}')
        table = model.__sql_table__
        if table.name in tables:
            raise ValueError(f'Duplicate model table: {table.name}')
        tables[table.name] = table

    engine = None
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            inspector = sa_inspect(conn)
            actual_tables = set(inspector.get_table_names())
            drift = [f'Missing table: {name}' for name in sorted(set(tables) - actual_tables)]
            # Unselected tables are not assumed to belong to this model set.
            unverified = []
            for name in sorted(set(tables) & actual_tables):
                changes, unknown = _compare_table(inspector, tables[name])
                drift.extend(changes)
                unverified.extend(unknown)
    except (SQLAlchemyError, ImportError, OSError):
        raise ValueError(f'Unable to validate MySQL database alias {alias!r}; verify connectivity and permissions') from None
    finally:
        if engine is not None:
            engine.dispose()
    status = 'DRIFT' if drift else 'UNVERIFIED' if unverified else 'MATCH'
    return status, tuple(drift), tuple(unverified)
