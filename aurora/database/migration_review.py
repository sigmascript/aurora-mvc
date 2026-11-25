"""Migration plans and review artifacts; these documents never authorize execution.

All formats and fingerprint rules remain unchanged. No database DDL runs here.
"""

import hashlib
import hmac
import json
import re
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .snapshots import compare_snapshots
from .schema import _quote
from .schema import validate_init_models
from .migration_timeline import inspect_server_timeline
from decimal import Decimal, InvalidOperation
from datetime import datetime
import sqlite3


# Migration plan definitions and deterministic snapshot identity.
_PLAN_FORMAT = "aurorasql.migration.v1"
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}\Z")
_ORDER = {
    "added_table": 0,
    "added_column": 1,
    "changed_column": 2,
    "removed_column": 3,
    "removed_table": 4,
}
_RISK = {"additive", "requires_data_strategy", "potential_data_loss"}


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def snapshot_fingerprint(snapshot):
    """Hash validated declarations, excluding advisory SQL previews."""
    compare_snapshots(snapshot, snapshot)
    declaration = {
        "format": snapshot["format"],
        "dialect": snapshot["dialect"],
        "tables": [
            {"name": table["name"], "columns": sorted(table["columns"], key=lambda c: c["name"])}
            for table in sorted(snapshot["tables"], key=lambda t: t["name"])
        ],
    }
    return hashlib.sha256(_canonical(declaration).encode("utf-8")).hexdigest()


def validate_migration_plan(plan):
    """Fail closed on malformed plans; validation does not approve execution."""
    keys = {
        "format",
        "id",
        "dialect",
        "before_fingerprint",
        "after_fingerprint",
        "operations",
        "execution_allowed",
    }

    if not isinstance(plan, dict) or set(plan) != keys or plan["format"] != _PLAN_FORMAT:
        raise ValueError("Invalid migration plan format")

    if not isinstance(plan["id"], str) or not _IDENTIFIER.fullmatch(plan["id"]):
        raise ValueError("Invalid migration identifier")

    if plan["dialect"] not in ("sqlite", "mysql", "postgresql"):
        raise ValueError("Invalid migration dialect")

    for key in ("before_fingerprint", "after_fingerprint"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{64}", plan[key]):
            raise ValueError("Invalid schema fingerprint")

    if plan["execution_allowed"] is not False or not isinstance(plan["operations"], list):
        raise ValueError("Migration plans are review-only")

    for operation in plan["operations"]:
        if not isinstance(operation, dict) or set(operation) != {
            "kind",
            "table",
            "column",
            "risk",
            "requires_review",
        }:
            raise ValueError("Invalid migration operation")

        kind = operation["kind"]

        if kind not in _ORDER or operation["risk"] not in _RISK:
            raise ValueError("Unsupported migration operation")

        if not isinstance(operation["table"], str) or not _IDENTIFIER.fullmatch(operation["table"]):
            raise ValueError("Invalid table identifier")

        column = operation["column"]

        if kind in ("added_table", "removed_table"):
            if column is not None:
                raise ValueError("Table operation cannot name a column")
        elif not isinstance(column, str) or not _IDENTIFIER.fullmatch(column):
            raise ValueError("Invalid column identifier")

        if operation["requires_review"] is not True:
            raise ValueError("Every operation requires review")

    return plan


def migration_plan(before, after, *, migration_id):
    """Compare snapshots into an ordered, read-only migration proposal."""

    if not isinstance(migration_id, str) or not _IDENTIFIER.fullmatch(migration_id):
        raise ValueError("Invalid migration identifier")

    changes = compare_snapshots(before, after)
    operations = sorted(
        (
            {
                "kind": item["kind"],
                "table": item["table"],
                "column": item["column"],
                "risk": item["risk"],
                "requires_review": True,
            }
            for item in changes
        ),
        key=lambda op: (_ORDER[op["kind"]], op["table"], op["column"] or ""),
    )
    return validate_migration_plan(
        {
            "format": _PLAN_FORMAT,
            "id": migration_id,
            "dialect": before["dialect"],
            "before_fingerprint": snapshot_fingerprint(before),
            "after_fingerprint": snapshot_fingerprint(after),
            "operations": operations,
            "execution_allowed": False,
        }
    )


def migration_plan_json(plan):
    """Serialize a validated proposal deterministically."""
    validate_migration_plan(plan)
    return json.dumps(plan, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


# Review artifacts bind the validated plan to its source and target snapshots.
_REVIEW_FORMAT = "aurorasql.review.v1"


def _canonical_review(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def create_review(before, after, *, migration_id):
    """Create a stable, read-only proposal bound to both schema snapshots."""
    plan = migration_plan(before, after, migration_id=migration_id)
    document = {
        "format": _REVIEW_FORMAT,
        "before": before,
        "after": after,
        "plan": plan,
        "execution_allowed": False,
    }
    digest = hashlib.sha256(_canonical_review(document)).hexdigest()
    return {**document, "review_sha256": digest}


def validate_review(review):
    """Reject tampering, mismatched snapshots, and execution permission."""
    required = {"format", "before", "after", "plan", "execution_allowed", "review_sha256"}

    if (
        not isinstance(review, dict)
        or set(review) != required
        or review["format"] != _REVIEW_FORMAT
    ):
        raise ValueError("Invalid migration review artifact")

    if review["execution_allowed"] is not False:
        raise ValueError("Migration review cannot authorize execution")

    plan = validate_migration_plan(review["plan"])
    expected = migration_plan(review["before"], review["after"], migration_id=plan["id"])

    if plan != expected:
        raise ValueError("Review plan differs from the snapshots")

    digest = review["review_sha256"]

    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Invalid review fingerprint")

    body = {key: review[key] for key in required if key != "review_sha256"}

    if not hmac.compare_digest(hashlib.sha256(_canonical_review(body)).hexdigest(), digest):
        raise ValueError("Review fingerprint mismatch")

    return review


def verify_review_acknowledgment(review, *, reviewed_sha256):
    """Confirm human-reviewed identity only; never grant DDL permission."""
    validate_review(review)

    if not isinstance(reviewed_sha256, str) or not hmac.compare_digest(
        review["review_sha256"], reviewed_sha256
    ):
        raise PermissionError("Exact reviewed SHA-256 fingerprint required")

    return True


def review_json(review):
    validate_review(review)
    return json.dumps(review, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def save_review(review, destination):
    """Explicitly create a new review file; never overwrite or create directories."""
    payload = review_json(review)
    path = Path(destination)

    if path.is_symlink():
        raise FileExistsError("Review destination must not be a symbolic link")

    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(payload)

    return path


def load_review(source):
    with Path(source).open("r", encoding="utf-8") as stream:
        return validate_review(json.load(stream))


def migration_title(explicit, default):
    """Choose a bounded title without blocking scripts or noninteractive shells."""
    if explicit is None:
        explicit = input(f"Migration title [{default}]: ") if sys.stdin.isatty() else ""

    if not isinstance(explicit, str):
        raise ValueError("Migration title must be text")

    if any(ord(char) < 32 or ord(char) == 127 for char in explicit):
        raise ValueError("Migration title contains control characters")

    title = explicit.strip() or default

    if len(title) > 160 or any(ord(char) < 32 or ord(char) == 127 for char in title):
        raise ValueError("Migration title is too long or contains control characters")

    return title


# Additive-column proposals are read-only evidence, not execution approval.


@dataclass(frozen=True)
class AddColumnReview:
    """Immutable review token for exactly one nullable column addition."""

    migration_id: str
    dialect: str
    table: str
    column: str
    plan_fingerprint: str
    sql_preview: str
    execution_allowed: bool = False


def _column_sql(field, dialect):
    """Render a deliberately restricted nullable-column declaration."""

    if (
        field["primary_key"]
        or not field["nullable"]
        or field["unique"]
        or field["references"] is not None
        or field["server_default"] is not None
    ):
        raise ValueError("Only plain nullable columns without constraints are reviewable")

    kind = field["type"]
    length = field["length"]

    if kind == "str":
        sql_type = f"VARCHAR({length})" if length is not None else "TEXT"
    elif kind == "int":
        sql_type = "INTEGER" if dialect == "sqlite" else "BIGINT"
    elif kind == "float":
        sql_type = {"sqlite": "REAL", "mysql": "DOUBLE", "postgresql": "DOUBLE PRECISION"}[dialect]
    elif kind == "bool":
        sql_type = "INTEGER" if dialect == "sqlite" else "BOOLEAN"
    elif kind == "date":
        sql_type = "DATE"
    elif kind == "datetime":
        sql_type = "DATETIME" if dialect == "mysql" else "TIMESTAMP"
    else:
        raise ValueError("Unsupported column type")

    if kind != "str" and length is not None:
        raise ValueError("Unexpected length on non-string column")

    return sql_type


def review_add_column(connection, before, after, plan):
    """Review a single additive column change; do not authorize or execute DDL.

    The existing preflight verifies column presence but not all live column
    definitions. This function therefore must not be used as an executor gate.
    """
    validate_migration_plan(plan)
    expected = migration_plan(before, after, migration_id=plan["id"])

    if expected != plan:
        raise ValueError("Migration plan does not match supplied snapshots")

    if len(plan["operations"]) != 1:
        raise ValueError("Review requires exactly one operation")

    operation = plan["operations"][0]

    if operation["kind"] != "added_column" or operation["risk"] != "additive":
        raise ValueError("Only additive column changes can be reviewed")

    from .migration_safety import preflight_migration

    preflight = preflight_migration(connection, before, after, plan)
    blockers = [item for item in preflight.findings if not item.startswith("Read-only preflight:")]

    if blockers:
        raise RuntimeError("Migration preflight findings: " + "; ".join(blockers))

    table = next(item for item in after["tables"] if item["name"] == operation["table"])
    field = next(item for item in table["columns"] if item["name"] == operation["column"])
    sql_type = _column_sql(field, connection.dialect)
    sql = (
        f"ALTER TABLE {_quote(operation['table'], connection.dialect)} "
        f"ADD COLUMN {_quote(operation['column'], connection.dialect)} {sql_type}"
    )
    serialized = json.dumps(plan, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return AddColumnReview(
        plan["id"],
        connection.dialect,
        operation["table"],
        operation["column"],
        sha256(serialized.encode()).hexdigest(),
        sql,
    )


# Reviewed JSON schema-operation format and strict normalization.
_FORMAT = "aurorasql.operations.v1"
_EXPLICIT_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}\Z")
_MAX_BYTES = 256 * 1024
_RESERVED_PREFIX = "sqlite_"
_RESERVED_TABLES = frozenset(
    {
        "aurorasql_migration_intents_v2",
        "aurorasql_schema_history",
        "aurorasql_additive_history_v1",
    }
)


def _identifier(value):
    if not isinstance(value, str) or not _EXPLICIT_IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid schema-operation identifier")

    if value.casefold().startswith(_RESERVED_PREFIX):
        raise ValueError("Reserved SQLite schema identifier")

    return value


def _unique_names(names):
    normalized = [name.casefold() for name in names]

    if len(set(normalized)) != len(normalized):
        raise ValueError("Duplicate or case-colliding schema identifiers")


def _unique_json_pairs(items):
    result = {}

    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key in schema operations")

        result[key] = value

    return result


def _reject_constant(value):
    raise ValueError("Non-JSON numeric constant is not supported")


def load_explicit_operations(source):
    """Load one reviewed structural operation with a strict normalized shape."""
    path = Path(source).expanduser()

    if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_BYTES:
        raise ValueError("Operations file must be an existing small regular file")

    try:
        raw = path.read_bytes()

        if len(raw) > _MAX_BYTES:
            raise ValueError("Operations file is too large")

        document = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_pairs,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Cannot decode operations JSON") from error

    if not isinstance(document, dict) or document.get("format", _FORMAT) != _FORMAT:
        raise ValueError("Unsupported schema-operation format")

    alias = _identifier(document.get("database"))
    operations = set(document) - {"format", "database"}

    if operations == {"rename_table"}:
        mapping = document["rename_table"]

        if not isinstance(mapping, dict) or len(mapping) != 1:
            raise ValueError("rename_table requires exactly one old/new table pair")

        original, replacement = next(iter(mapping.items()))
        _identifier(original)
        _identifier(replacement)
        _unique_names((original, replacement))

        if original.casefold() in _RESERVED_TABLES or replacement.casefold() in _RESERVED_TABLES:
            raise ValueError("Migration journals cannot be renamed")

        normalized = {"kind": "rename_table", "table": original, "new_table": replacement}
    elif operations == {"rename_columns"}:
        tables = document["rename_columns"]

        if not isinstance(tables, dict) or len(tables) != 1:
            raise ValueError("rename_columns requires exactly one target table")

        table, changes = next(iter(tables.items()))
        _identifier(table)

        if (
            table.casefold() in _RESERVED_TABLES
            or not isinstance(changes, dict)
            or not 1 <= len(changes) <= 20
        ):
            raise ValueError("Invalid rename_columns target or column count")

        for original, replacement in changes.items():
            _identifier(original)
            _identifier(replacement)
            _unique_names((original, replacement))

        _unique_names(changes.keys())
        _unique_names(changes.values())

        if {key.casefold() for key in changes} & {value.casefold() for value in changes.values()}:
            raise ValueError("Chained or cyclic column renames require separate operations")

        normalized = {
            "kind": "rename_columns",
            "table": table,
            "columns": dict(sorted(changes.items())),
        }
    elif operations == {"create_index"}:
        spec = document["create_index"]

        if not isinstance(spec, dict) or set(spec) - {"table", "name", "columns", "unique"}:
            raise ValueError("create_index has unsupported fields")

        table = _identifier(spec.get("table"))
        name = _identifier(spec.get("name"))
        columns = spec.get("columns")
        unique = spec.get("unique", False)

        if (
            table.casefold() in _RESERVED_TABLES
            or not isinstance(columns, list)
            or not 1 <= len(columns) <= 16
            or type(unique) is not bool
        ):
            raise ValueError("Invalid create_index specification")

        for column in columns:
            _identifier(column)

        _unique_names(columns)
        normalized = {
            "kind": "create_index",
            "table": table,
            "name": name,
            "columns": columns,
            "unique": unique,
        }
    elif operations == {"drop_index"}:
        spec = document["drop_index"]
        if not isinstance(spec, dict) or set(spec) != {"table", "name"}:
            raise ValueError("drop_index requires exactly 'table' and 'name'")
        table, name = _identifier(spec.get("table")), _identifier(spec.get("name"))
        if table.casefold() in _RESERVED_TABLES:
            raise ValueError("Migration history indexes cannot be changed")
        normalized = {"kind": "drop_index", "table": table, "name": name}
    else:
        raise ValueError("Exactly one supported schema operation is required")

    return {"format": _FORMAT, "database": alias, "operation": normalized}


def _fingerprint(document, catalog, dialect, database_path=None):
    body = {
        "document": document,
        "catalog": catalog,
        "dialect": dialect,
        "database_identity": str(database_path) if database_path else None,
    }
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_forward_explicit_sqlite(db_file, root, models, operation, statements):
    """Review forward-only SQLite history and rehearse exact model correspondence.

    The database file is opened only read-only. SQL is run against an in-memory
    copy; this function neither approves nor performs a persistent migration.
    """
    import sqlite3
    from contextlib import closing

    from .constraints import matches_models_with_indexes, validate_index_operation
    from .migration_history import sqlite_read_history as read_history
    from .schema_inspection import _catalog

    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as observed:
        read_history(observed, root, db_file)
        if operation["kind"] in ("create_index", "drop_index"):
            validate_index_operation(observed, models, operation)
    expected = _rehearse_sqlite(db_file, statements)
    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as source:
        clone = sqlite3.connect(":memory:")
        try:
            source.backup(clone)
            clone.execute("PRAGMA foreign_keys=ON")
            clone.execute("PRAGMA legacy_alter_table=OFF")
            for sql in statements:
                clone.execute(sql)
            matches_models_with_indexes(clone, models)
            if operation["kind"] not in ("create_index", "drop_index"):
                if clone.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Rename would leave invalid foreign-key references")
            if _catalog(clone) != expected:
                raise RuntimeError("Rehearsed schema differs from expected result")
        finally:
            clone.close()


# Server review fallback produces only structured, credential-safe diagnostics.
class UnsupportedServerChange(ValueError):
    """A reviewed refusal with a fixed, credential-safe public explanation."""


def review_forward_server_change(
    connection, models, alias, identity, *, reviewer, widening_reviewer
):
    """Select a reviewed forward change, sanitizing unsafe diagnostic details."""
    try:
        proposal = reviewer(connection, models, alias, identity)
    except ValueError as primary_error:
        try:
            proposal = widening_reviewer(connection, models, alias, identity)
        except ValueError as fallback_error:
            # The model reviewer checks live values before refusing a narrowing.
            # Relay only its strictly structured, credential-free refusal; never
            # print arbitrary driver exceptions, SQL, or connection strings.
            reason = str(primary_error)
            if re.fullmatch(
                r"Cannot narrow [A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]* "
                r"to VARCHAR\([0-9]+\): existing value length [0-9]+ exceeds the target",
                reason,
            ):
                raise UnsupportedServerChange(
                    reason + ". No database changes were made."
                ) from primary_error
            reasons = {
                "DECIMAL precision/scale cannot be reduced": "DECIMAL narrowing is not supported; preserve or increase precision and scale.",
            }
            explanation = reasons.get(
                str(fallback_error),
                "Only one table's verified compatible additions, widening, defaults, or nullability changes are supported.",
            )
            raise UnsupportedServerChange(explanation) from fallback_error
    return proposal


def _rehearse_sqlite(db_file, statements):
    """Rehearse on a bounded in-memory same-engine copy, preserving all rows."""
    import sqlite3
    from contextlib import closing

    from .schema_inspection import _catalog

    with closing(sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True)) as source:
        pages = source.execute("PRAGMA page_count").fetchone()[0]
        size = source.execute("PRAGMA page_size").fetchone()[0]

        if pages * size > 64 * 1024 * 1024:
            raise ValueError("Rehearsal database exceeds the bounded 64 MiB limit")

        clone = sqlite3.connect(":memory:")

        try:
            source.backup(clone)
            clone.execute("PRAGMA foreign_keys=ON")
            clone.execute("PRAGMA legacy_alter_table=OFF")

            for statement in statements:
                clone.execute(statement)

            if clone.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("Staging migration violates foreign-key constraints")

            if clone.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise RuntimeError("Staging migration integrity check failed")

            return _catalog(clone)
        finally:
            clone.close()


def verify_server_initial_current(connection, models, alias):
    """Verify the single native Initial against models, legacy intent and schema.

    This is bounded to the supported initial schema shape. Any successor,
    unresolved journal, unknown catalog object or model drift returns False.
    It never initializes, reconciles or modifies history.
    """
    from hashlib import sha256

    from .migration_history import list_history, review_history
    from .model_discovery import model_identity_map
    from .schema import create_table_sql
    from .schema_inspection import verify_server_initial_schema

    timeline = inspect_server_timeline(connection, alias)

    if timeline is None or len(timeline) != 1 or timeline[0].status != "applied":
        return False

    initial = timeline[0]

    if initial.model_tables != model_identity_map(models):
        return False

    history = list_history(connection)

    if review_history(connection, entries=history) or len(history) != len(models):
        return False

    expected = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode("utf-8")
        ).hexdigest()
        for model in models
    }

    if {entry.table for entry in history} != set(expected) or any(
        entry.status != "applied"
        or entry.table not in expected
        or entry.dialect != connection.dialect
        or entry.fingerprint != expected[entry.table]
        for entry in history
    ):
        return False

    # A journal trigger could change history while preserving the table/row
    # shape. Reject these before trusting an applied record for another DDL.
    if connection.dialect == "mysql":
        triggers = connection.fetch_all(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() "
            "AND EVENT_OBJECT_TABLE IN ('aurorasql_schema_history', 'aurorasql_migration_timeline')"
        )
    else:
        triggers = connection.fetch_all(
            "SELECT t.tgname AS name FROM pg_catalog.pg_trigger t "
            "WHERE t.tgrelid IN (to_regclass('aurorasql_schema_history'), "
            "to_regclass('aurorasql_migration_timeline')) AND NOT t.tgisinternal"
        )

    if triggers:
        return False

    try:
        digest = verify_server_initial_schema(connection, models)
    except (ValueError, RuntimeError):
        return False

    return digest == initial.schema_sha256


def verify_server_successor_current(connection, models, alias):
    """Verify one applied new-table or column successor against its catalog.

    A timeline status alone never establishes schema equality. Require both
    historical and current model mappings, legacy per-table journal evidence,
    and the original Initial fingerprint computed while verifying *all* tables.
    """
    from hashlib import sha256

    from .migration_history import list_history, review_history
    from .model_discovery import model_identity_map
    from .schema import create_table_sql
    from .schema_inspection import verify_server_declared_schema
    from .schema_planning import (
        column_addition_predecessors,
        default_addition_predecessor_models,
        nullability_predecessor_models,
        widening_predecessor_models,
    )

    timeline = inspect_server_timeline(connection, alias)

    if (
        timeline is None
        or len(timeline) != 2
        or any(entry.status != "applied" for entry in timeline)
    ):
        return False

    initial, successor = timeline
    current = model_identity_map(models)
    previous = initial.model_tables

    operation = successor.operation

    if (
        successor.model_tables != current
        or not isinstance(operation, dict)
        or any(key.startswith("untrusted:") for key in current)
    ):
        return False

    is_addition = operation.get("kind") == "add_column"
    is_widening = operation.get("kind") == "widen_column"
    is_nullability = operation.get("kind") == "change_nullability"
    is_default = operation.get("kind") == "add_default"

    if is_addition:
        if current != previous:
            return False

        try:
            original_models = column_addition_predecessors(models, operation)
            target = next(model for model in models if model.__table_name__ == operation["table"])
            alter = _add_column_statement(target, operation["column"], connection.dialect)
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif is_widening:
        if current != previous:
            return False

        try:
            original_models = widening_predecessor_models(models, operation)
            from .schema_planning import _declared_type, _normalized_declaration

            target = next(model for model in models if model.__table_name__ == operation["table"])
            field = target.__columns__[operation["column"]]
            if (
                _normalized_declaration(
                    _declared_type(field, connection.dialect), connection.dialect
                )
                != operation["to"]
            ):
                return False

            old_v = _VARCHAR.fullmatch(operation["from"])
            new_v = _VARCHAR.fullmatch(operation["to"])
            old_d = _DECIMAL.fullmatch(operation["from"])
            new_d = _DECIMAL.fullmatch(operation["to"])

            if old_v and new_v:
                if int(old_v[1]) >= int(new_v[1]):
                    return False
            elif old_d and new_d:
                old_p, old_s = map(int, old_d.groups())
                new_p, new_s = map(int, new_d.groups())
                if (
                    new_s < old_s
                    or new_p - new_s < old_p - old_s
                    or (new_p, new_s) == (old_p, old_s)
                ):
                    return False
            else:
                return False

            table_name = _quote(operation["table"], connection.dialect)
            column_name = _quote(operation["column"], connection.dialect)
            declared_type = _declared_type(field, connection.dialect)
            if connection.dialect == "postgresql":
                alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE {declared_type}"
            else:
                nullability = " NULL" if field.nullable else " NOT NULL"
                alter = f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} {declared_type}{nullability}"
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif is_nullability:
        if current != previous:
            return False

        try:
            original_models = nullability_predecessor_models(models, operation)
            target = next(model for model in models if model.__table_name__ == operation["table"])
            field = target.__columns__[operation["column"]]
            table_name = _quote(operation["table"], connection.dialect)
            column_name = _quote(operation["column"], connection.dialect)

            if connection.dialect == "postgresql":
                action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
                alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} {action}"
            else:
                from .schema_planning import _declared_type

                nullability = "NULL" if field.nullable else "NOT NULL"
                alter = (
                    f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} "
                    f"{_declared_type(field, connection.dialect)} {nullability}"
                )
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif is_default:
        if current != previous:
            return False

        try:
            original_models = default_addition_predecessor_models(models, operation)
            target = next(model for model in models if model.__table_name__ == operation["table"])
            from .schema import _default_literal

            literal = _default_literal(target.__columns__[operation["column"]], connection.dialect)

            if literal is None:
                return False

            table_name = _quote(operation["table"], connection.dialect)
            column_name = _quote(operation["column"], connection.dialect)
            alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET DEFAULT {literal}"
        except (KeyError, StopIteration, ValueError, TypeError):
            return False
    elif operation.get("kind") == "create_table":
        if len(current) != len(previous) + 1 or any(
            current.get(key) != value for key, value in previous.items()
        ):
            return False

        added = set(current) - set(previous)

        if len(added) != 1 or operation.get("table") != current[next(iter(added))]:
            return False

        original_models = [
            model for model in models if f"{model.__module__}:{model.__qualname__}" in previous
        ]
    else:
        return False

    if len(original_models) != len(previous):
        return False

    journal = list_history(connection)

    if review_history(
        connection, entries=journal, verified_successor_table=operation["table"]
    ) or len(journal) != len(models) + int(
        is_addition or is_widening or is_nullability or is_default
    ):
        return False

    expected = {
        model.__table_name__: sha256(
            create_table_sql(model, connection.dialect).encode("utf-8")
        ).hexdigest()
        for model in (
            original_models
            if (is_addition or is_widening or is_nullability or is_default)
            else models
        )
    }

    if is_addition or is_widening or is_nullability or is_default:
        expected_records = {(table, fingerprint) for table, fingerprint in expected.items()}
        expected_records.add((operation["table"], sha256(alter.encode("utf-8")).hexdigest()))
    else:
        expected_records = {(table, fingerprint) for table, fingerprint in expected.items()}

    if (
        len(expected_records) != len(journal)
        or {(entry.table, entry.fingerprint) for entry in journal} != expected_records
        or any(
            entry.status != "applied" or entry.dialect != connection.dialect for entry in journal
        )
    ):
        return False

    if connection.dialect == "mysql":
        triggers = connection.fetch_all(
            "SELECT TRIGGER_NAME AS name FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() "
            "AND EVENT_OBJECT_TABLE IN ('aurorasql_schema_history', 'aurorasql_migration_timeline')"
        )
    elif connection.dialect == "postgresql":
        triggers = connection.fetch_all(
            "SELECT t.tgname AS name FROM pg_catalog.pg_trigger t "
            "WHERE t.tgrelid IN (to_regclass('aurorasql_schema_history'), "
            "to_regclass('aurorasql_migration_timeline')) AND NOT t.tgisinternal"
        )
    else:
        return False

    if triggers:
        return False

    try:
        if is_addition:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection, models, historical_exclusion=(operation["table"], operation["column"])
            )
        elif is_widening:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection,
                models,
                historical_type_override=(
                    operation["table"],
                    operation["column"],
                    operation["from"],
                ),
            )
        elif is_nullability:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection,
                models,
                historical_nullability_override=(
                    operation["table"],
                    operation["column"],
                    operation["from"],
                ),
            )
        elif is_default:
            successor_digest, initial_digest = verify_server_declared_schema(
                connection,
                models,
                historical_default_exclusion=(operation["table"], operation["column"]),
            )
        else:
            initial_digest = verify_server_declared_schema(
                connection, models, evidence_models=original_models
            )
            successor_digest = verify_server_declared_schema(connection, models)
    except (ValueError, RuntimeError):
        return False

    return initial_digest == initial.schema_sha256 and successor_digest == successor.schema_sha256


def _reviewed_server_successor_number(
    connection, models, alias, identity, review, initial_predecessor_verifier
):
    """Choose the next sequence only after complete prior-history verification.

    The first successor retains its original verified Initial proof. Later
    successors must verify the whole applied chain and physical predecessor.
    """
    from .model_discovery import model_identity_map

    timeline = inspect_server_timeline(connection, alias)

    if not timeline or any(entry.status != "applied" for entry in timeline):
        raise RuntimeError("Complete applied server history is required")

    if len(timeline) == 1:
        if timeline[0].model_tables != model_identity_map(models):
            raise RuntimeError("Verified original model history is required")

        initial_predecessor_verifier(connection, models, alias, review)
        return 2

    from .migration_history import verify_server_review_predecessor

    return verify_server_review_predecessor(connection, models, alias, identity, review)


# Native server model-change review and eligibility (read-only).

from .schema_inspection import inspect_table
from .schema_planning import _declared_type, _normalized_declaration, plan_schema
from .schema import _default_literal, create_table_sql
from .schema_inspection import server_default_matches, postgres_literal_value


_VARCHAR = re.compile(r"varchar\((\d+)\)\Z")
_DECIMAL = re.compile(r"decimal\((\d+),(\d+)\)\Z")


@dataclass(frozen=True)
class ServerModelProposal:
    dialect: str
    statements: tuple[str, ...]
    operations: tuple[dict, ...]
    review_sha256: str
    execution_allowed: bool = False


def _add_column_clause(model, field_name, dialect, *, connection=None):
    """Render a validated addition, checking empty-table required columns."""
    from .schema import _SQL_TYPES

    field = model.__columns__[field_name]

    if (
        field.primary_key
        or field.unique
        or field.references is not None
        or field.server_default is not None
    ):
        raise ValueError("New column requires an ordinary field; keys need separate review")

    if not field.nullable and field.default is None:
        if connection is None:
            raise ValueError(
                "New column requires a nullable field or a validated literal default; "
                "undefaulted required fields need verified empty-table review"
            )

        table_name = _quote(model.__table_name__, dialect)
        row = connection.fetch_first(f"SELECT 1 AS present FROM {table_name} LIMIT 1")

        if row is not None:
            raise ValueError(
                "New required column without a default requires an empty existing table"
            )

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
    return f"ALTER TABLE {_quote(model.__table_name__, dialect)} " + _add_column_clause(
        model, field_name, dialect
    )


def _verified_prior_default(field, observed, dialect):
    """Approve only known typed constants when changing an existing default."""
    if observed is None:
        return True
    if not isinstance(observed, str):
        return False
    if dialect == "postgresql":
        return postgres_literal_value(field, observed) is not None
    if field.python_type in (int, float) or field.python_type is Decimal:
        try:
            return Decimal(observed).is_finite()
        except InvalidOperation:
            return False
    if field.python_type is bool:
        return observed.lower() in ("0", "1", "true", "false")
    if field.python_type in (str,):
        return not bool(
            re.fullmatch(r"(?i)(?:current_timestamp|now|uuid)\s*\(.*\)|current_timestamp", observed)
        )
    return False


def _review_coherent_columns(connection, model, changes, table):
    """One-table, one-statement safe edits; no destructive conversions.

    The reviewed catalog is re-read while holding the migration lock. VARCHAR
    narrowing is verified against stored text lengths before every execution;
    default edits affect only future writes, not existing values.
    """
    from .schema_planning import _declared_type, _normalized_declaration

    dialect = connection.dialect
    name = model.__table_name__
    if not hasattr(table, "columns") or not hasattr(table, "constraints_verified"):
        raise ValueError(
            "Server additive migration cannot combine unsupported existing-column drift"
        )
    actual = {column.name: column for column in table.columns}
    primary_index = "PRIMARY" if dialect == "mysql" else f"{name}_pkey"
    if (
        not table.exists
        or not table.uniqueness_verified
        or not table.constraints_verified
        or any(index != primary_index for index in table.indexes)
    ):
        raise ValueError("Existing table requires verified ordinary indexes and constraints")

    by_column = {}
    for change in changes:
        if change.kind not in (
            "missing_column",
            "type_mismatch",
            "nullability_mismatch",
            "default_mismatch",
        ):
            raise ValueError("Server transition contains an unsupported schema difference")
        by_column.setdefault(change.column, set()).add(change.kind)

    clauses = []
    edits = []
    added = []
    for column, kinds in by_column.items():
        field = model.__columns__[column]
        if kinds == {"missing_column"}:
            clauses.append(_add_column_clause(model, column, dialect, connection=connection))
            added.append(column)
            continue
        if "missing_column" in kinds or column not in actual:
            raise ValueError("New column cannot be combined with an existing-column change")
        previous = actual[column]
        if (
            field.primary_key
            or field.unique
            or field.references is not None
            or field.server_default is not None
            or previous.primary_key
            or column in table.unique_columns
        ):
            raise ValueError("Existing key or dependent column needs constraint review")

        old_type = _normalized_declaration(previous.sql_type, dialect)
        declared_type = _declared_type(field, dialect)
        new_type = _normalized_declaration(declared_type, dialect)
        if "type_mismatch" in kinds:
            old_v, new_v = _VARCHAR.fullmatch(old_type), _VARCHAR.fullmatch(new_type)
            old_d, new_d = _DECIMAL.fullmatch(old_type), _DECIMAL.fullmatch(new_type)
            if old_v and new_v:
                target_length = int(new_v[1])
                existing_length = int(old_v[1])
                compatible = target_length != existing_length
                if compatible and target_length < existing_length:
                    row = connection.fetch_first(
                        f"SELECT MAX(CHAR_LENGTH({_quote(column, dialect)})) AS max_length "
                        f"FROM {_quote(name, dialect)}"
                    )
                    measured = None if row is None else row["max_length"]
                    if measured is None:
                        measured = 0
                    if int(measured) > target_length:
                        raise ValueError(
                            f"Cannot narrow {name}.{column} to VARCHAR({target_length}): "
                            f"existing value length {int(measured)} exceeds the target"
                        )
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
                        f"SELECT 1 AS found FROM {_quote(name, dialect)} "
                        f"WHERE ABS({col_sql}) >= {capacity} "
                        f"OR {col_sql} <> ROUND({col_sql}, {ns}) LIMIT 1"
                    )
                    if row is not None:
                        raise ValueError(
                            f"Cannot change {name}.{column} to DECIMAL({np},{ns}): "
                            "stored value exceeds integral capacity or needs rounding"
                        )
            else:
                compatible = False
            if not compatible:
                raise ValueError(
                    "Only verified VARCHAR length changes and compatible DECIMAL widening are supported"
                )
        elif old_type != new_type:
            raise ValueError("Existing column type has unreported schema drift")

        if not _verified_prior_default(field, previous.default_sql, dialect):
            raise ValueError(f"Existing SQL default for {name}.{column} is not a verified literal")
        if "default_mismatch" not in kinds and not server_default_matches(
            field, previous.default_sql, dialect
        ):
            raise ValueError("Existing default has unreported schema drift")
        literal = _default_literal(field, dialect)
        if dialect == "mysql" and declared_type.strip().upper() in ("TEXT", "BLOB") and literal:
            raise ValueError("MySQL TEXT/BLOB defaults require separate review")

        if "nullability_mismatch" in kinds:
            if previous.nullable == field.nullable:
                raise ValueError("Nullability change is no longer present")
            if not field.nullable:
                row = connection.fetch_first(
                    f"SELECT 1 AS found FROM {_quote(name, dialect)} "
                    f"WHERE {_quote(column, dialect)} IS NULL LIMIT 1"
                )
                if row is not None:
                    raise ValueError(f"Existing NULL values prevent NOT NULL on {name}.{column}")
        elif previous.nullable != field.nullable:
            raise ValueError("Existing nullability has unreported schema drift")

        col_sql = _quote(column, dialect)
        if dialect == "mysql":
            clause = f"MODIFY COLUMN {col_sql} {declared_type} "
            clause += "NULL" if field.nullable else "NOT NULL"
            if literal is not None:
                clause += f" DEFAULT {literal}"
            clauses.append(clause)
        else:
            if "type_mismatch" in kinds:
                clauses.append(f"ALTER COLUMN {col_sql} TYPE {declared_type}")
            if "nullability_mismatch" in kinds:
                action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
                clauses.append(f"ALTER COLUMN {col_sql} {action}")
            if "default_mismatch" in kinds:
                action = "DROP DEFAULT" if literal is None else f"SET DEFAULT {literal}"
                clauses.append(f"ALTER COLUMN {col_sql} {action}")
        edits.append(
            {
                "column": column,
                "changes": tuple(sorted(kinds)),
                "before": {
                    "type": old_type,
                    "nullable": previous.nullable,
                    "default": previous.default_sql,
                },
                "after": {"type": new_type, "nullable": field.nullable, "default": literal},
            }
        )

    if not edits:
        raise ValueError("Existing-column transition requires at least one compatible edit")
    operation = {
        "kind": "alter_columns",
        "table": name,
        "edits": tuple(edits),
        "additions": tuple(added),
    }
    statement = f"ALTER TABLE {_quote(name, dialect)} " + ", ".join(clauses)
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

    if (
        not isinstance(alias, str)
        or not alias
        or not isinstance(database_identity, str)
        or not database_identity
    ):
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

        if (
            not table.uniqueness_verified
            or table.indexes
            and any(index != expected_primary for index in table.indexes)
        ):
            raise ValueError("Indexed source table needs dedicated migration review")

        if all(change.kind == "missing_column" for change in changes):
            names = tuple(change.column for change in changes)
            clauses = [
                _add_column_clause(model, name, dialect, connection=connection) for name in names
            ]
            statement = f"ALTER TABLE {_quote(model.__table_name__, dialect)} " + ", ".join(clauses)
            operation = (
                {"kind": "add_column", "table": model.__table_name__, "column": names[0]}
                if len(names) == 1
                else {"kind": "add_columns", "table": model.__table_name__, "columns": names}
            )
        else:
            operation, statement = _review_coherent_columns(connection, model, changes, table)
        proposals.append((operation, statement))

    if len(proposals) != 1:
        raise ValueError("Server migration requires one verified table transition per execution")

    operations = tuple(item[0] for item in proposals)
    statements = tuple(item[1] for item in proposals)
    payload = json.dumps(
        {
            "dialect": dialect,
            "alias": alias,
            "identity": database_identity,
            "operations": operations,
            "statements": statements,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )

    return ServerModelProposal(
        dialect, statements, operations, sha256(payload.encode("utf-8")).hexdigest()
    )


# Read-only proposals for single-column default and nullability changes.


def review_server_default_addition(connection, models, alias, identity):
    """Propose one literal SQL DEFAULT from a provably default-free column."""
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql") or not alias or not identity:
        raise ValueError("A selected server database identity is required")

    ordered = validate_init_models(models, dialect=dialect)
    proposals = []

    for model in ordered:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if not plan.exists or len(plan.changes) != 1 or plan.changes[0].kind != "default_mismatch":
            raise ValueError("Mixed or existing-default edits require separate review")

        name = plan.changes[0].column
        field = model.__columns__[name]
        table = inspect_table(connection, model.__table_name__)
        observed = {column.name: column for column in table.columns}
        expected_primary = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if (
            not table.exists
            or not table.uniqueness_verified
            or name not in observed
            or field.primary_key
            or field.unique
            or field.references is not None
            or field.server_default is not None
            or field.default is None
            or observed[name].primary_key
            or name in table.unique_columns
            or observed[name].default_sql is not None
            or observed[name].nullable != field.nullable
            or any(index != expected_primary for index in table.indexes)
        ):
            raise ValueError("Existing/defaulted or constrained column requires manual review")

        if _normalized_declaration(observed[name].sql_type, dialect) != _normalized_declaration(
            _declared_type(field, dialect), dialect
        ):
            raise ValueError("Default addition cannot change the column type")

        literal = _default_literal(field, dialect)

        if literal is None:
            raise ValueError("A validated SQL default literal is required")

        table_sql = _quote(model.__table_name__, dialect)
        column_sql = _quote(name, dialect)
        statement = f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} SET DEFAULT {literal}"
        proposals.append(
            ({"kind": "add_default", "table": model.__table_name__, "column": name}, statement)
        )

    if len(proposals) != 1:
        raise ValueError("Exactly one verified SQL default addition is required")

    operations = (proposals[0][0],)
    statements = (proposals[0][1],)
    payload = json.dumps(
        {
            "dialect": dialect,
            "alias": alias,
            "identity": identity,
            "operations": operations,
            "statements": statements,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return ServerModelProposal(
        dialect, statements, operations, sha256(payload.encode("utf-8")).hexdigest()
    )


def review_server_nullability_change(connection, models, alias, identity):
    """Propose a single plain-column change; refuse unsafe non-null tightening."""
    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql") or not alias or not identity:
        raise ValueError("A selected server database identity is required")

    ordered = validate_init_models(models, dialect=dialect)
    proposals = []

    for model in ordered:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if (
            not plan.exists
            or len(plan.changes) != 1
            or plan.changes[0].kind != "nullability_mismatch"
        ):
            raise ValueError("Mixed or unsafe nullability changes require separate review")

        name = plan.changes[0].column
        field = model.__columns__[name]
        table = inspect_table(connection, model.__table_name__)
        actual = {column.name: column for column in table.columns}
        expected_primary = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if (
            not table.exists
            or not table.uniqueness_verified
            or name not in actual
            or field.primary_key
            or field.unique
            or field.references is not None
            or field.default is not None
            or field.server_default is not None
            or actual[name].primary_key
            or name in table.unique_columns
            or actual[name].default_sql is not None
            or any(index != expected_primary for index in table.indexes)
        ):
            raise ValueError("Constrained or indexed nullability change is unsupported")

        declaration = _declared_type(field, dialect)

        if _normalized_declaration(actual[name].sql_type, dialect) != _normalized_declaration(
            declaration, dialect
        ):
            raise ValueError("Nullability edit cannot change the column type")

        if actual[name].nullable == field.nullable:
            raise ValueError("No nullability change is present")

        table_sql = _quote(model.__table_name__, dialect)
        column_sql = _quote(name, dialect)

        if not field.nullable:
            # An indexed read can be used by the server, but the result must
            # remain bounded to one row. Recheck after taking the DDL lock.
            existing_null = connection.fetch_first(
                f"SELECT 1 AS found FROM {table_sql} WHERE {column_sql} IS NULL LIMIT 1"
            )

            if existing_null is not None:
                raise ValueError("Existing NULL rows prevent a NOT NULL change")

        if dialect == "postgresql":
            action = "DROP NOT NULL" if field.nullable else "SET NOT NULL"
            statement = f"ALTER TABLE {table_sql} ALTER COLUMN {column_sql} {action}"
        else:
            nullability = "NULL" if field.nullable else "NOT NULL"
            statement = (
                f"ALTER TABLE {table_sql} MODIFY COLUMN {column_sql} {declaration} {nullability}"
            )

        proposals.append(
            (
                {
                    "kind": "change_nullability",
                    "table": model.__table_name__,
                    "column": name,
                    "from": actual[name].nullable,
                    "to": field.nullable,
                },
                statement,
            )
        )

    if len(proposals) != 1:
        raise ValueError("Exactly one verified nullability change is required")

    operations = (proposals[0][0],)
    statements = (proposals[0][1],)
    payload = json.dumps(
        {
            "dialect": dialect,
            "alias": alias,
            "identity": identity,
            "operations": operations,
            "statements": statements,
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return ServerModelProposal(
        dialect, statements, operations, sha256(payload.encode("utf-8")).hexdigest()
    )


def review_server_column_widening(connection, models, alias, identity):
    """Review precisely one widening without changing data or catalog state."""
    from .schema import validate_init_models

    dialect = connection.dialect

    if dialect not in ("mysql", "postgresql") or not alias or not identity:
        raise ValueError("A selected server database identity is required")

    ordered = validate_init_models(models, dialect=dialect)
    candidates = []

    for model in ordered:
        plan = plan_schema(connection, model)

        if not plan.changes:
            continue

        if not plan.exists or len(plan.changes) != 1 or plan.changes[0].kind != "type_mismatch":
            raise ValueError("Mixed or unsafe server column changes require separate review")

        name = plan.changes[0].column
        field = model.__columns__[name]
        table = inspect_table(connection, model.__table_name__)
        actual = {column.name: column for column in table.columns}

        if (
            not table.uniqueness_verified
            or name not in actual
            or field.primary_key
            or field.unique
            or field.references is not None
            or field.server_default is not None
            or field.default is not None
            or actual[name].default_sql is not None
            or actual[name].nullable != field.nullable
            or actual[name].primary_key
            or name in table.unique_columns
        ):
            raise ValueError("Indexed or constrained column widening is unsupported")

        allowed_index = "PRIMARY" if dialect == "mysql" else f"{model.__table_name__}_pkey"

        if any(index != allowed_index for index in table.indexes):
            raise ValueError("Additional indexes require dedicated widening verification")

        old = _normalized_declaration(actual[name].sql_type, dialect)
        new = _normalized_declaration(_declared_type(field, dialect), dialect)
        old_varchar, new_varchar = _VARCHAR.fullmatch(old), _VARCHAR.fullmatch(new)
        old_decimal, new_decimal = _DECIMAL.fullmatch(old), _DECIMAL.fullmatch(new)

        if old_varchar and new_varchar:
            if int(new_varchar[1]) <= int(old_varchar[1]):
                raise ValueError("VARCHAR narrowing or unchanged length is not supported")
        elif old_decimal and new_decimal:
            old_precision, old_scale = map(int, old_decimal.groups())
            new_precision, new_scale = map(int, new_decimal.groups())

            if (
                new_scale < old_scale
                or new_precision - new_scale < old_precision - old_scale
                or (new_scale == old_scale and new_precision == old_precision)
            ):
                raise ValueError("DECIMAL precision/scale cannot be reduced")
        else:
            raise ValueError("Only compatible VARCHAR or DECIMAL widening is supported")

        table_name = _quote(model.__table_name__, dialect)
        column_name = _quote(name, dialect)
        sql_type = _declared_type(field, dialect)

        if dialect == "postgresql":
            statement = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE {sql_type}"
        else:
            nullability = " NULL" if field.nullable else " NOT NULL"
            default = _default_literal(field, dialect)
            statement = (
                f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} {sql_type}{nullability}"
            )

            if default is not None:
                statement += f" DEFAULT {default}"

        candidates.append(
            (
                {
                    "kind": "widen_column",
                    "table": model.__table_name__,
                    "column": name,
                    "from": old,
                    "to": new,
                },
                statement,
            )
        )

    if len(candidates) != 1:
        raise ValueError("Exactly one verified widening candidate is required")

    operation, statement = candidates[0]
    payload = json.dumps(
        {
            "dialect": dialect,
            "alias": alias,
            "identity": identity,
            "operations": (operation,),
            "statements": (statement,),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return ServerModelProposal(
        dialect, (statement,), (operation,), sha256(payload.encode("utf-8")).hexdigest()
    )


# Read-only SQLite legacy-chain review: no conversion or schema mutation occurs.


def sqlite_legacy_initial_review(raw, root, db_file, models, alias):
    """Review an alpha history only when EVERY step has an exact full snapshot.

    The old chain is independently verified before matching its immutable steps
    with timestamped schema witnesses. Missing or ambiguous evidence refuses
    conversion; it is never reconstructed from current models.
    """
    from .connections import Connection
    from .migration_history import (
        _SQLITE_SNAPSHOT,
        SQLITE_LEGACY_TABLE,
        SQLITE_TABLE,
        _sqlite_physical,
        _sqlite_tables,
    )
    from .migration_review import sqlite_matches_models as _sqlite_matches_models
    from .migration_timeline import inspect_native_history, verify_current_models
    from .snapshots import prepare_schema_record, schema_snapshot

    tables = _sqlite_tables(raw)
    if tables & {SQLITE_TABLE, SQLITE_LEGACY_TABLE} or "aurorasql_migration_timeline" not in tables:
        return None
    timeline, entries, findings = inspect_native_history(Connection(raw, "sqlite"), alias)
    if not timeline or findings or not timeline[-1]["physical_history_consistent"]:
        return None
    if raw.in_transaction:
        from .model_discovery import model_identity_map

        if timeline[-1]["model_tables"] != model_identity_map(models):
            return None
        try:
            _sqlite_matches_models(raw, models)
        except (ValueError, RuntimeError):
            return None
    elif not verify_current_models(raw, models, timeline, entries):
        return None

    directory, _ = prepare_schema_record(root, db_file, models)
    if not directory.is_dir() or directory.is_symlink():
        return None
    snapshots = []
    for path in sorted(
        directory.iterdir(),
        key=lambda item: (
            item.name[:15],
            int(item.stem[16:]) if item.stem[15:16] == "-" and item.stem[16:].isdigit() else -1,
        ),
    ):
        if path.is_symlink() or not path.is_file() or not _SQLITE_SNAPSHOT.fullmatch(path.name):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        snapshots.append((path, record))

    # Refuse extra, missing, duplicated, or unordered witnesses rather than
    # silently guessing which historical record belongs to which migration.
    if len(snapshots) != len(timeline):
        return None
    expected_current = schema_snapshot(*models, dialect="sqlite")["tables"]
    proposal = []
    previous_schema = None
    for item, (path, record) in zip(timeline, snapshots):
        physical = record.get("physical_schema")
        declared = record.get("models")
        if (
            record.get("engine") != "sqlite"
            or record.get("database") != Path(db_file).stem
            or record.get("title") != item["title"]
            or not isinstance(physical, list)
            or not isinstance(declared, list)
            or not physical
            or not declared
            or {table.get("name") for table in declared if isinstance(table, dict)}
            != set(item["model_tables"].values())
            or {
                obj.get("name")
                for obj in physical
                if isinstance(obj, dict) and obj.get("type") == "table"
            }
            != {table["name"] for table in declared}
            or physical == previous_schema
        ):
            return None
        previous_schema = physical
        try:
            date = datetime.strptime(path.name[:15], "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            return None
        proposal.append((item["title"], path.name, date))
    if (
        snapshots[-1][1]["physical_schema"] != _sqlite_physical(raw)
        or snapshots[-1][1]["models"] != expected_current
    ):
        return None
    return tuple(proposal)


# SQLite read-only schema-change reviews (shared review owner; SQLite-specific guards).
from .constraints import _ordinary_index_sql
from .migration_history import sqlite_read_history as read_history, _sqlite_physical as _physical


def _data_witness(raw, table):
    """Fingerprint complete table contents, independent of row retrieval order.

    This is only review evidence: execution compares it again while holding the
    SQLite writer lock, before any DDL.
    """
    columns = len(raw.execute(f"PRAGMA table_info({_quote(table, 'sqlite')})").fetchall())
    if not columns:
        raise RuntimeError("Deletion target has no verifiable columns")
    ordering = ", ".join(str(i) for i in range(1, columns + 1))
    digest = hashlib.sha256()
    for row in raw.execute(f"SELECT * FROM {_quote(table, 'sqlite')} ORDER BY {ordering}"):
        for value in row:
            encoded = (type(value).__name__ + ":" + repr(value)).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def review_table_removal(raw, root, db_file, models, *, allow_new_tables=False):
    """Return one independently evidenced table removal, or refuse.

    A table is eligible when no surviving table references it and no views or
    triggers can be affected. Its own outgoing foreign keys disappear with it.
    This is inspection, not execution authorization.
    """
    _, snapshot = read_history(raw, root, db_file)
    prior = {entry["name"] for entry in snapshot["models"]}
    current = {model.__table_name__ for model in models}
    removed = prior - current
    if len(removed) != 1 or (current - prior and not allow_new_tables):
        raise UnsafeTableTransition(
            "Table removal requires exactly one undeclared table; "
            "added tables require a mixed review"
        )
    target = removed.pop()
    catalog = _physical(raw)
    # Ordinary indexes owned by the removed table go with that table.
    # Keep the validated index catalog for unrelated application tables.
    try:
        _ordinary_index_sql(raw)
    except ValueError as exc:
        raise UnsafeTableTransition(
            "Table removal has unverified ordinary-index dependencies"
        ) from exc
    if any(item["type"] in ("view", "trigger") for item in catalog):
        raise UnsafeTableTransition(
            f"Cannot remove {target}: views or triggers require separate review"
        )
    for item in catalog:
        if item["type"] != "table":
            continue
        for key in raw.execute(f"PRAGMA foreign_key_list({_quote(item['name'], 'sqlite')})"):
            if item["name"] != target and key[2].casefold() == target.casefold():
                raise UnsafeTableTransition(
                    f"Cannot remove {target}: surviving table {item['name']} references it"
                )
    # Count inside the writer's IMMEDIATE transaction again before execution.
    count = raw.execute(f"SELECT COUNT(*) FROM {_quote(target, 'sqlite')}").fetchone()[0]
    return target, count, _data_witness(raw, target)


def review_column_removal(raw, root, db_file, models, *, allow_mixed=False):
    """Review an entire set of removed columns against real dependencies and data."""
    from .connections import Connection
    from .schema_planning import plan_schema

    if not allow_mixed and sqlite3.sqlite_version_info < (3, 35, 0):
        raise ValueError("SQLite column deletion requires SQLite 3.35 or newer")
    _, previous = read_history(raw, root, db_file)
    prior = {entry["name"] for entry in previous["models"]}
    current = {model.__table_name__ for model in models}
    if current != prior:
        raise ValueError("Column removal cannot be combined with table changes")
    changes = []
    connection = Connection(raw, "sqlite")
    trusted = {
        item["name"]
        for item in previous["physical_schema"]
        if item["type"] == "index" and item["sql"] is not None
    }
    for model in models:
        plan = plan_schema(connection, model, models=models, trusted_indexes=trusted)
        if plan.warnings:
            raise ValueError("Column removal has unsupported schema warnings")
        changes.extend((model.__table_name__, change) for change in plan.changes)
    if not changes or not any(change.kind == "extra_column" for _, change in changes):
        raise ValueError("Column removal needs a removed column")
    if allow_mixed:
        allowed = {
            "extra_column",
            "type_mismatch",
            "default_mismatch",
            "nullability_mismatch",
            "unique_mismatch",
            "foreign_key_mismatch",
            "foreign_key_action_mismatch",
            "missing_column",
        }
        if (
            any(change.kind not in allowed for _, change in changes)
            or len({table for table, _ in changes}) != 1
        ):
            raise ValueError("Mixed column removal requires one supported table transition")
    elif any(change.kind != "extra_column" for _, change in changes):
        raise ValueError("Column removal must contain only removed columns")
    try:
        indexes = _ordinary_index_sql(raw)
    except ValueError as exc:
        raise UnsafeTableTransition("Column removal has unverified index dependencies") from exc
    if any(item["type"] in ("view", "trigger") for item in _physical(raw)):
        raise UnsafeTableTransition("Column removal has view or trigger dependencies")
    # Recheck every target before changing a single column. Reject any
    # dependency that refers to a removed column, not unrelated FK/indexes.
    reviewed = []
    for table, change in changes:
        if change.kind != "extra_column":
            continue
        column = change.column
        for index_name, index_table, _ in indexes:
            if index_table != table:
                continue
            keys = [
                entry[2]
                for entry in raw.execute(f"PRAGMA index_xinfo({_quote(index_name, 'sqlite')})")
                if entry[5]
            ]
            if column in keys:
                raise UnsafeTableTransition(
                    f"Cannot remove {table}.{column}: ordinary index {index_name} uses this column"
                )
        for item in _physical(raw):
            if item["type"] != "table":
                continue
            for fk in raw.execute(f"PRAGMA foreign_key_list({_quote(item['name'], 'sqlite')})"):
                if (item["name"] == table and fk[3] == column) or (
                    fk[2].casefold() == table.casefold() and fk[4] == column
                ):
                    raise UnsafeTableTransition(
                        f"Cannot remove {table}.{column}: foreign-key dependency in {item['name']}"
                    )
        count = raw.execute(
            f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')} "
            f"WHERE {_quote(column, 'sqlite')} IS NOT NULL"
        ).fetchone()[0]
        reviewed.append((table, column, count, _data_witness(raw, table)))
    return tuple(reviewed)


class UnsafeTableTransition(ValueError):
    """An internally verified, developer-safe table-transition refusal."""


def _verify_sqlite_varchar_length(raw, table, column, target_length):
    """Reject incompatible stored values without writing or exposing their contents."""
    # SQLite length() stops at NUL, so inspect decoded values.
    for (value,) in raw.execute(
        f"SELECT {_quote(column, 'sqlite')} FROM {_quote(table, 'sqlite')} "
        f"WHERE {_quote(column, 'sqlite')} IS NOT NULL"
    ):
        if not isinstance(value, str):
            raise UnsafeTableTransition(f"Cannot narrow {table}.{column}: stored value is not text")
        if len(value) > target_length:
            raise UnsafeTableTransition(
                f"Cannot narrow {table}.{column} to VARCHAR({target_length}): "
                f"an existing value has {len(value)} characters; "
                "truncation would occur. No database changes were made"
            )


def sqlite_narrowing_warnings(connection, model, plan):
    """Assess only VARCHAR narrowing candidates in a read-only schema plan."""
    from .schema_planning import _sqlite_declared_type

    type_changes = [change for change in plan.changes if change.kind == "type_mismatch"]
    if not type_changes:
        return []

    actual = {
        column.name: column.sql_type for column in inspect_table(connection, plan.table).columns
    }
    warnings = []
    for change in type_changes:
        column = change.column
        existing = actual.get(column, "").upper().replace(" ", "")
        desired = _sqlite_declared_type(model.__columns__[column]).upper().replace(" ", "")
        old = re.fullmatch(r"VARCHAR\((\d+)\)", existing)
        new = re.fullmatch(r"VARCHAR\((\d+)\)", desired)
        if not old or not new or int(new[1]) >= int(old[1]):
            continue

        try:
            _verify_sqlite_varchar_length(connection._raw, plan.table, column, int(new[1]))
        except UnsafeTableTransition as error:
            warnings.append(str(error))
        except (sqlite3.Error, UnicodeError):
            warnings.append(
                f"Cannot verify stored text lengths for {plan.table}.{column}; "
                "narrowing safety is unverified."
            )

    return warnings


def _incoming_foreign_keys(raw, parent_table):
    """Return columns on surviving tables that reference the selected parent."""
    references = []

    for item in _physical(raw):
        if item["type"] != "table" or item["name"] == parent_table:
            continue

        child = item["name"]
        keys = raw.execute(f"PRAGMA foreign_key_list({_quote(child, 'sqlite')})").fetchall()

        for key in keys:
            if key[2].casefold() == parent_table.casefold():
                references.append((child, key[4]))

    return references


def _review_ordinary_rebuild(raw, root, db_file, models, kind):
    """Review an ordinary SQLite table transition and its real data risks.

    One table may have several type/default changes. The same review runs
    read-only before confirmation and again inside the write transaction.
    """
    from .connections import Connection
    from .schema import _default_literal
    from .schema_planning import _sqlite_declared_type, plan_schema

    _, prior = read_history(raw, root, db_file)
    trusted = {
        item["name"]
        for item in prior["physical_schema"]
        if item["type"] == "index" and item["sql"] is not None
    }
    changes = []
    for model in models:
        plan = plan_schema(Connection(raw, "sqlite"), model, models=models, trusted_indexes=trusted)
        if plan.warnings or not plan.exists:
            return None
        changes.extend((model, change) for change in plan.changes)
    allowed = (
        {
            "type_mismatch",
            "default_mismatch",
            "nullability_mismatch",
            "unique_mismatch",
            "foreign_key_mismatch",
            "foreign_key_action_mismatch",
            "missing_column",
            "extra_column",
        }
        if kind == "table_transition"
        else {kind}
    )
    if not changes or any(change.kind not in allowed for _, change in changes):
        return None
    tables = {model.__table_name__ for model, _ in changes}
    if len(tables) != 1:
        return None
    table = next(iter(tables))
    model = next(model for model in models if model.__table_name__ == table)
    names = tuple(sorted({change.column for _, change in changes}))
    actual_columns = {
        row[1]: row[2] for row in raw.execute(f"PRAGMA table_info({_quote(table, 'sqlite')})")
    }
    if any(change.kind == "extra_column" for _, change in changes):
        # Reuse the destructive reviewer for actual dependencies and row risks.
        review_column_removal(raw, root, db_file, models, allow_mixed=True)
    for _, change in changes:
        name = change.column
        if change.kind == "extra_column":
            continue
        field = model.__columns__[name]
        if change.kind == "missing_column":
            from .migration_execution import _sqlite_additive_column_sql

            # A rebuild can introduce an undefaulted required column only
            # when no existing row needs a value for that column.
            required_without_default = not field.nullable and field.default is None

            if (
                required_without_default
                and raw.execute(f"SELECT 1 FROM {_quote(table, 'sqlite')} LIMIT 1").fetchone()
            ):
                raise UnsafeTableTransition(
                    f"Cannot add {table}.{name}: populated rows require a value "
                    "before NOT NULL can be applied"
                )

            _sqlite_additive_column_sql(model, name, allow_empty_required=required_without_default)
            continue
        if (
            field.primary_key
            or field.server_default is not None
            or (
                field.references is not None
                and change.kind not in ("foreign_key_mismatch", "foreign_key_action_mismatch")
            )
        ):
            raise UnsafeTableTransition(
                f"Cannot change {table}.{name}: key or constrained columns require separate review"
            )
        if change.kind in ("foreign_key_mismatch", "foreign_key_action_mismatch"):
            # SQLite permits a nullable FK and its removal; existing rows must
            # satisfy the target before a new or redirected FK is installed.
            # The model resolver independently establishes unique/PK target and
            # matching source/target types; the catalog/history are verified.
            if field.references is not None:
                from .schema import _resolved_foreign_keys

                target_table, target_column, _, _ = _resolved_foreign_keys(model, models)[name]
                missing = raw.execute(
                    f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')} AS source "
                    f"WHERE source.{_quote(name, 'sqlite')} IS NOT NULL "
                    f"AND NOT EXISTS (SELECT 1 FROM {_quote(target_table, 'sqlite')} AS target "
                    f"WHERE target.{_quote(target_column, 'sqlite')} = "
                    f"source.{_quote(name, 'sqlite')})"
                ).fetchone()[0]
                if missing:
                    raise UnsafeTableTransition(
                        f"Cannot reference {target_table}.{target_column} from {table}.{name}: "
                        f"{missing} existing value(s) have no referenced row. "
                        "No database changes were made"
                    )
            continue
        if change.kind == "unique_mismatch":
            if field.unique:
                # A single-column UNIQUE is safe only when existing non-NULL
                # values are actually unique. SQLite permits multiple NULLs.
                duplicated = raw.execute(
                    f"SELECT 1 FROM {_quote(table, 'sqlite')} "
                    f"WHERE {_quote(name, 'sqlite')} IS NOT NULL "
                    f"GROUP BY {_quote(name, 'sqlite')} HAVING COUNT(*) > 1 LIMIT 1"
                ).fetchone()
                if duplicated is not None:
                    raise UnsafeTableTransition(
                        f"Cannot make {table}.{name} UNIQUE: existing non-NULL values "
                        "contain duplicates. No database changes were made"
                    )
            continue
        if change.kind == "default_mismatch":
            _default_literal(field, "sqlite")
            continue
        if change.kind == "nullability_mismatch":
            if not field.nullable:
                missing = raw.execute(
                    f"SELECT COUNT(*) FROM {_quote(table, 'sqlite')} "
                    f"WHERE {_quote(name, 'sqlite')} IS NULL"
                ).fetchone()[0]
                if missing:
                    raise UnsafeTableTransition(
                        f"Cannot require {table}.{name}: {missing} stored NULL value(s) "
                        "need a value before NOT NULL can be applied. "
                        "No database changes were made"
                    )
            continue
        existing = actual_columns.get(name, "").upper().replace(" ", "")
        desired = _sqlite_declared_type(field).upper().replace(" ", "")
        old_varchar = re.fullmatch(r"VARCHAR\((\d+)\)", existing)
        new_varchar = re.fullmatch(r"VARCHAR\((\d+)\)", desired)
        if old_varchar and new_varchar:
            if int(new_varchar[1]) < int(old_varchar[1]):
                _verify_sqlite_varchar_length(raw, table, name, int(new_varchar[1]))
        elif existing == "INTEGER" and new_varchar:
            limit = int(new_varchar[1])
            for (value,) in raw.execute(
                f"SELECT {_quote(name, 'sqlite')} FROM {_quote(table, 'sqlite')} "
                f"WHERE {_quote(name, 'sqlite')} IS NOT NULL"
            ):
                if type(value) is not int or len(str(value)) > limit:
                    raise UnsafeTableTransition(
                        f"Cannot convert {table}.{name} from INTEGER to VARCHAR({limit}): "
                        "at least one stored value is incompatible or too long. "
                        "No database changes were made"
                    )
        elif old_varchar and desired == "INTEGER":
            for (value,) in raw.execute(
                f"SELECT {_quote(name, 'sqlite')} FROM {_quote(table, 'sqlite')} "
                f"WHERE {_quote(name, 'sqlite')} IS NOT NULL"
            ):
                if (
                    not isinstance(value, str)
                    or not re.fullmatch(r"-?(?:0|[1-9][0-9]*)", value)
                    or len(value) > 20
                ):
                    raise UnsafeTableTransition(
                        f"Cannot convert {table}.{name} from VARCHAR to INTEGER: "
                        "stored text is not a canonical integer. No database changes were made"
                    )
                number = int(value)
                if not -(2**63) <= number < 2**63:
                    raise UnsafeTableTransition(
                        f"Cannot convert {table}.{name} to INTEGER: stored value "
                        "exceeds the SQLite integer range. No database changes were made"
                    )
        else:
            raise UnsafeTableTransition(
                f"Cannot change {table}.{name} from {existing} to {desired}: "
                "this conversion has not been verified for lossless execution"
            )
    indexes = [
        (idx_name, sql)
        for idx_name, idx_table, sql in _ordinary_index_sql(raw)
        if idx_table == table
    ]
    if any(item["type"] in ("trigger", "view") for item in _physical(raw)):
        raise UnsafeTableTransition("Table transition has view or trigger dependencies")
    incoming = _incoming_foreign_keys(raw, table)

    if incoming:
        # A UNIQUE-only rebuild may leave existing child references intact,
        # but it must never modify a column referenced by those children.
        if kind != "unique_mismatch" or any(
            not referenced_column or referenced_column.casefold() == names[0].casefold()
            for _, referenced_column in incoming
        ):
            raise UnsafeTableTransition("Table transition has inbound foreign-key dependencies")

    if any(
        key[2].casefold() == table.casefold()
        for key in raw.execute(f"PRAGMA foreign_key_list({_quote(table, 'sqlite')})")
    ):
        raise UnsafeTableTransition("Self-referencing foreign keys need separate review")
    target = names if kind == "table_transition" else names[0]
    return (
        table,
        target,
        tuple(indexes),
        tuple((item["type"], item["name"], item["sql"]) for item in prior["physical_schema"]),
    )


def review_default_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "default_mismatch")


def review_nullability_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "nullability_mismatch")


def review_unique_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "unique_mismatch")


def review_length_change(raw, root, db_file, models):
    return _review_ordinary_rebuild(raw, root, db_file, models, "type_mismatch")


def review_table_transition(raw, root, db_file, models):
    """Inspect a single coherent SQLite table transition before approval."""
    return _review_ordinary_rebuild(raw, root, db_file, models, "table_transition")


# SQLite forward-history ownership (R4.15).
def sqlite_matches_models(raw, models):
    """Compare physical structure independently, without trusting a snapshot alone."""
    import sqlite3

    from .migration_execution import _verify_result_catalog
    from .schema import create_table_sql

    # Reuse the physical comparison, but it expects only application objects.
    # A temporary in-memory clone removes the internal migrations table from
    # the comparison; no application rows are read or copied.
    with sqlite3.connect(":memory:") as expected:
        expected.execute("PRAGMA foreign_keys=ON")
        for model in models:
            expected.execute(create_table_sql(model, "sqlite", models=models))
        with sqlite3.connect(":memory:") as observed:
            for item in _physical(raw):
                if item["type"] == "table":
                    observed.execute(item["sql"])
            _verify_result_catalog(observed, expected, {model.__table_name__ for model in models})
    return True
