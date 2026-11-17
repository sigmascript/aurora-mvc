"""Read-only MySQL/PostgreSQL model-change eligibility review.

An eligible proposal is not execution authorization. Server-side DDL needs
separate history, backup, locking and postcondition guarantees.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from hashlib import sha256
import json

from .inspection import inspect_table
from .init_validation import validate_init_models
from .planning import plan_schema
from .schema import _default_literal, _quote, create_table_sql
from .server_initial_verification import server_default_matches, postgres_literal_value


@dataclass(frozen=True)
class ServerModelProposal:
    dialect: str
    statements: tuple[str, ...]
    operations: tuple[dict, ...]
    review_sha256: str
    execution_allowed: bool = False


def _add_column_clause(model, field_name, dialect):
    """Render one validated, literal-only additive column clause."""
    from .schema import _SQL_TYPES

    field = model.__columns__[field_name]

    if (field.primary_key or field.unique or field.references is not None
            or field.server_default is not None
            or (not field.nullable and field.default is None)):
        raise ValueError("New column requires a nullable field or a validated literal default; keys need separate review")

    if field.sql_type is not None:
        sql_type = field.sql_type
    elif field.python_type.__name__ == "date":
        sql_type = "DATE"
    elif field.python_type.__name__ == "datetime":
        sql_type = "DATETIME" if dialect == "mysql" else "TIMESTAMP"
    elif field.python_type is str and field.length is not None:
        sql_type = f"VARCHAR({field.length})"
    else:
        sql_type = _SQL_TYPES[dialect][field.python_type]

    default = _default_literal(field, dialect)
    if dialect == "mysql" and sql_type.strip().upper() in ("TEXT", "BLOB") and default is not None:
        raise ValueError("MySQL TEXT/BLOB defaults require separate backend-specific review")
    declaration = f"{_quote(field_name, dialect)} {sql_type}"

    if default is not None:
        declaration += f" DEFAULT {default}"
    if not field.nullable:
        declaration += " NOT NULL"

    return f"ADD COLUMN {declaration}"


def _add_column_statement(model, field_name, dialect):
    """Retain the historical single-column DDL reconstruction contract."""
    return (f"ALTER TABLE {_quote(model.__table_name__, dialect)} "
            + _add_column_clause(model, field_name, dialect))



def _verified_prior_default(field, observed, dialect):
    """Approve only known typed constants when changing an existing default."""
    if observed is None:
        return True
    if not isinstance(observed, str):
        return False
    if dialect == 'postgresql':
        return postgres_literal_value(field, observed) is not None
    if field.python_type in (int, float) or field.python_type is Decimal:
        try:
            return Decimal(observed).is_finite()
        except InvalidOperation:
            return False
    if field.python_type is bool:
        return observed.lower() in ('0', '1', 'true', 'false')
    if field.python_type in (str,):
        return not bool(re.fullmatch(
            r'(?i)(?:current_timestamp|now|uuid)\s*\(.*\)|current_timestamp',
            observed))
    return False


def _review_coherent_columns(connection, model, changes, table):
    """One-table, one-statement safe edits; no destructive conversions.

    The reviewed catalog is re-read while holding the migration lock. VARCHAR
    narrowing is verified against stored text lengths before every execution;
    default edits affect only future writes, not existing values.
    """
    from .planning import _declared_type, _normalized_declaration
    from .server_column_widening import _VARCHAR, _DECIMAL

    dialect = connection.dialect
    name = model.__table_name__
    if not hasattr(table, 'columns') or not hasattr(table, 'constraints_verified'):
        raise ValueError('Server additive migration cannot combine unsupported existing-column drift')
    actual = {column.name: column for column in table.columns}
    primary_index = 'PRIMARY' if dialect == 'mysql' else f'{name}_pkey'
    if (not table.exists or not table.uniqueness_verified or
            not table.constraints_verified or
            any(index != primary_index for index in table.indexes)):
        raise ValueError('Existing table requires verified ordinary indexes and constraints')

    by_column = {}
    for change in changes:
        if change.kind not in ('missing_column', 'type_mismatch',
                               'nullability_mismatch', 'default_mismatch'):
            raise ValueError('Server transition contains an unsupported schema difference')
        by_column.setdefault(change.column, set()).add(change.kind)

    clauses = []
    edits = []
    added = []
    for column, kinds in by_column.items():
        field = model.__columns__[column]
        if kinds == {'missing_column'}:
            clauses.append(_add_column_clause(model, column, dialect))
            added.append(column)
            continue
        if 'missing_column' in kinds or column not in actual:
            raise ValueError('New column cannot be combined with an existing-column change')
        previous = actual[column]
        if (field.primary_key or field.unique or field.references is not None
                or field.server_default is not None or previous.primary_key
                or column in table.unique_columns):
            raise ValueError('Existing key or dependent column needs constraint review')

        old_type = _normalized_declaration(previous.sql_type, dialect)
        declared_type = _declared_type(field, dialect)
        new_type = _normalized_declaration(declared_type, dialect)
        if 'type_mismatch' in kinds:
            old_v, new_v = _VARCHAR.fullmatch(old_type), _VARCHAR.fullmatch(new_type)
            old_d, new_d = _DECIMAL.fullmatch(old_type), _DECIMAL.fullmatch(new_type)
            if old_v and new_v:
                target_length = int(new_v[1])
                existing_length = int(old_v[1])
                compatible = target_length != existing_length
                if compatible and target_length < existing_length:
                    row = connection.fetch_first(
                        f'SELECT MAX(CHAR_LENGTH({_quote(column, dialect)})) AS max_length '
                        f'FROM {_quote(name, dialect)}')
                    measured = None if row is None else row['max_length']
                    if measured is None:
                        measured = 0
                    if int(measured) > target_length:
                        raise ValueError(
                            f'Cannot narrow {name}.{column} to VARCHAR({target_length}): '
                            f'existing value length {int(measured)} exceeds the target')
            elif old_d and new_d:
                op, os = map(int, old_d.groups())
                np, ns = map(int, new_d.groups())
                compatible = (np, ns) != (op, os)
                # Reducing either the scale or integral capacity is safe only
                # when all real stored values fit the complete target type.
                if compatible and (ns < os or np - ns < op - os):
                    capacity = "1" + "0" * (np - ns)
                    col_sql = _quote(column, dialect)
                    row = connection.fetch_first(
                        f'SELECT 1 AS found FROM {_quote(name, dialect)} '
                        f'WHERE ABS({col_sql}) >= {capacity} '
                        f'OR {col_sql} <> ROUND({col_sql}, {ns}) LIMIT 1')
                    if row is not None:
                        raise ValueError(
                            f'Cannot change {name}.{column} to DECIMAL({np},{ns}): '
                            'stored value exceeds integral capacity or needs rounding')
            else:
                compatible = False
            if not compatible:
                raise ValueError('Only verified VARCHAR length changes and compatible DECIMAL widening are supported')
        elif old_type != new_type:
            raise ValueError('Existing column type has unreported schema drift')

        if not _verified_prior_default(field, previous.default_sql, dialect):
            raise ValueError(f'Existing SQL default for {name}.{column} is not a verified literal')
        if 'default_mismatch' not in kinds and not server_default_matches(
                field, previous.default_sql, dialect):
            raise ValueError('Existing default has unreported schema drift')
        literal = _default_literal(field, dialect)
        if dialect == 'mysql' and declared_type.strip().upper() in ('TEXT', 'BLOB') and literal:
            raise ValueError('MySQL TEXT/BLOB defaults require separate review')

        if 'nullability_mismatch' in kinds:
            if previous.nullable == field.nullable:
                raise ValueError('Nullability change is no longer present')
            if not field.nullable:
                row = connection.fetch_first(
                    f'SELECT 1 AS found FROM {_quote(name, dialect)} '
                    f'WHERE {_quote(column, dialect)} IS NULL LIMIT 1')
                if row is not None:
                    raise ValueError(f'Existing NULL values prevent NOT NULL on {name}.{column}')
        elif previous.nullable != field.nullable:
            raise ValueError('Existing nullability has unreported schema drift')

        col_sql = _quote(column, dialect)
        if dialect == 'mysql':
            clause = f'MODIFY COLUMN {col_sql} {declared_type} '
            clause += 'NULL' if field.nullable else 'NOT NULL'
            if literal is not None:
                clause += f' DEFAULT {literal}'
            clauses.append(clause)
        else:
            if 'type_mismatch' in kinds:
                clauses.append(f'ALTER COLUMN {col_sql} TYPE {declared_type}')
            if 'nullability_mismatch' in kinds:
                action = 'DROP NOT NULL' if field.nullable else 'SET NOT NULL'
                clauses.append(f'ALTER COLUMN {col_sql} {action}')
            if 'default_mismatch' in kinds:
                action = 'DROP DEFAULT' if literal is None else f'SET DEFAULT {literal}'
                clauses.append(f'ALTER COLUMN {col_sql} {action}')
        edits.append({'column': column, 'changes': tuple(sorted(kinds)),
                      'before': {'type': old_type, 'nullable': previous.nullable,
                                 'default': previous.default_sql},
                      'after': {'type': new_type, 'nullable': field.nullable,
                                'default': literal}})

    if not edits:
        raise ValueError('Existing-column transition requires at least one compatible edit')
    operation = {'kind': 'alter_columns', 'table': name,
                 'edits': tuple(edits), 'additions': tuple(added)}
    statement = f'ALTER TABLE {_quote(name, dialect)} ' + ', '.join(clauses)
    return operation, statement


def review_server_model_changes(connection, models, alias, database_identity):
    """Review one table's verified coherent ordinary transition as one DDL.

    Multiple columns in the same table are emitted in one ALTER TABLE,
    avoiding partial MySQL success between independently executed statements.
    No cross-table/mixed destructive changes are authorized by this reviewer.
    """
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql"):
        raise ValueError("Server model review requires MySQL or PostgreSQL")

    if (not isinstance(alias, str) or not alias or
            not isinstance(database_identity, str) or not database_identity):
        raise ValueError("Selected database identity and alias are required")

    ordered = validate_init_models(models, dialect=dialect)
    proposals = []

    for model in ordered:
        plan = plan_schema(connection, model)
        changes = plan.changes

        if not changes:
            continue

        if not plan.exists:
            if len(changes) != 1 or changes[0].kind != "create_table":
                raise ValueError("Unsupported missing-table schema change")

            statement = create_table_sql(model, dialect)
            proposals.append(({"kind": "create_table", "table": model.__table_name__}, statement))
            continue

        if plan.warnings:
            raise ValueError("Server transition requires verified physical schema metadata")

        # Index and constraint reflection is not complete enough to prove a
        # complex ALTER safe. Keep this narrow path for plain source tables.
        table = inspect_table(connection, model.__table_name__)

        expected_primary = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if not table.uniqueness_verified or table.indexes and any(
                index != expected_primary for index in table.indexes):
            raise ValueError("Indexed source table needs dedicated migration review")

        if all(change.kind == "missing_column" for change in changes):
            names = tuple(change.column for change in changes)
            clauses = [_add_column_clause(model, name, dialect) for name in names]
            statement = f"ALTER TABLE {_quote(model.__table_name__, dialect)} " + ", ".join(clauses)
            operation = ({"kind": "add_column", "table": model.__table_name__, "column": names[0]}
                         if len(names) == 1 else
                         {"kind": "add_columns", "table": model.__table_name__, "columns": names})
        else:
            operation, statement = _review_coherent_columns(connection, model, changes, table)
        proposals.append((operation, statement))

    if len(proposals) != 1:
        raise ValueError("Server migration requires one verified table transition per execution")

    operations = tuple(item[0] for item in proposals)
    statements = tuple(item[1] for item in proposals)
    payload = json.dumps({"dialect": dialect, "alias": alias, "identity": database_identity,
                          "operations": operations, "statements": statements},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)

    return ServerModelProposal(dialect, statements, operations,
                               sha256(payload.encode("utf-8")).hexdigest())
