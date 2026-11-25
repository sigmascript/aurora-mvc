"""Deterministic, read-only model-schema descriptions for human review.

Snapshots are JSON-compatible descriptions, not executable migrations.
No live database is inspected and Python defaults are never invoked.
"""

import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .schema import _default_literal, create_table_sql, _resolved_foreign_keys
from .models import parse_sql_type


_FORMAT = "aurorasql.schema.v1"
_FORMAT_V2 = "aurorasql.schema.v2"
_DIALECTS = ("sqlite", "mysql", "postgresql")
_COLUMN_KEYS = frozenset(
    {
        "name",
        "type",
        "length",
        "primary_key",
        "nullable",
        "unique",
        "references",
        "python_default",
        "server_default",
    }
)


def _default_description(value):
    if callable(value):
        return {"kind": "callable", "name": f"{value.__module__}.{value.__qualname__}"}

    if isinstance(value, (date, datetime)):
        return {"kind": "literal", "value": value.isoformat()}

    if value is None or type(value) in (str, int, float, bool):
        return {"kind": "literal", "value": value}

    raise TypeError("Unsupported model default for snapshot")


def schema_snapshot(*models, dialect, allow_v2=False):
    """Describe model declarations in stable order, without database access."""

    if not models:
        raise ValueError("At least one model is required")

    extended = any(
        field.model_reference or field.on_update != "NO ACTION" or field.on_delete != "NO ACTION"
        for model in models
        for field in model.__columns__.values()
    )
    if extended and not allow_v2:
        raise ValueError(
            "Extended foreign keys require explicit v2 snapshot review; legacy migration path refused"
        )
    tables = []
    names = set()

    for model in models:
        ddl = create_table_sql(model, dialect, models=models if extended else None)
        resolved = _resolved_foreign_keys(model, models) if extended else {}
        name = model.__table_name__

        if name in names:
            raise ValueError(f"Duplicate model table: {name}")

        names.add(name)
        columns = []

        for field_name, field in sorted(model.__columns__.items()):
            declaration = {
                "name": field_name,
                "type": "sql:" + field.sql_type if field.sql_type else field.python_type.__name__,
                "length": field.length,
                "primary_key": field.primary_key,
                "nullable": field.nullable,
                "unique": field.unique,
                "references": field.references,
                # Retain the v1 snapshot keys for existing review artifacts,
                # while representing new constant defaults as SQL-side intent.
                "python_default": _default_description(None),
                "server_default": _default_literal(field, dialect) or field.server_default,
            }
            if extended:
                target = resolved.get(field_name)
                declaration["references"] = f"{target[0]}.{target[1]}" if target else None
                declaration["on_update"] = target[2] if target else None
                declaration["on_delete"] = target[3] if target else None
            columns.append(declaration)

        tables.append({"name": name, "columns": columns, "create_sql_preview": ddl})

    return {
        "format": _FORMAT_V2 if extended else _FORMAT,
        "dialect": dialect,
        "tables": sorted(tables, key=lambda table: table["name"]),
    }


def _snapshot_tables(snapshot):
    """Validate historical v1 evidence; migration executors reject v2."""

    if not isinstance(snapshot, dict) or snapshot.get("format") != _FORMAT:
        raise ValueError("Unsupported schema snapshot format")

    if snapshot.get("dialect") not in _DIALECTS:
        raise ValueError("Unsupported schema snapshot dialect")

    if not isinstance(snapshot.get("tables"), list):
        raise ValueError("Invalid snapshot tables")

    result = {}

    for table in snapshot["tables"]:
        if (
            not isinstance(table, dict)
            or not isinstance(table.get("name"), str)
            or not table["name"]
        ):
            raise ValueError("Invalid snapshot table")

        if table["name"] in result:
            raise ValueError("Duplicate snapshot table")

        if not isinstance(table.get("columns"), list):
            raise ValueError("Invalid snapshot columns")

        columns = {}

        for field in table["columns"]:
            if not isinstance(field, dict) or set(field) != _COLUMN_KEYS:
                raise ValueError("Invalid snapshot column")

            name = field["name"]

            if not isinstance(name, str) or not name:
                raise ValueError("Invalid snapshot column name")

            if name in columns:
                raise ValueError("Duplicate snapshot column")

            if field["type"] not in ("str", "int", "float", "bool", "date", "datetime"):
                if not isinstance(field["type"], str) or not field["type"].startswith("sql:"):
                    raise ValueError("Invalid snapshot column type")

                canonical, _ = parse_sql_type(field["type"][4:])

                if field["type"] != "sql:" + canonical:
                    raise ValueError("Noncanonical snapshot SQL type")

            if field["length"] is not None and (
                type(field["length"]) is not int or field["length"] <= 0
            ):
                raise ValueError("Invalid snapshot column length")

            if any(type(field[key]) is not bool for key in ("primary_key", "nullable", "unique")):
                raise ValueError("Invalid snapshot column flags")

            if field["references"] is not None and not isinstance(field["references"], str):
                raise ValueError("Invalid snapshot column reference")

            if field["server_default"] is not None and not isinstance(field["server_default"], str):
                raise ValueError("Invalid snapshot server default")

            default = field["python_default"]

            if not isinstance(default, dict) or default.get("kind") not in ("literal", "callable"):
                raise ValueError("Invalid snapshot Python default")

            if default["kind"] == "callable":
                if set(default) != {"kind", "name"} or not isinstance(default["name"], str):
                    raise ValueError("Invalid snapshot callable default")
            elif set(default) != {"kind", "value"} or not (
                default["value"] is None or type(default["value"]) in (str, int, float, bool)
            ):
                raise ValueError("Invalid snapshot literal default")
            # create_sql_preview is intentionally excluded: it is an advisory
            # rendering, not the canonical schema declaration.
            columns[name] = {key: field[key] for key in _COLUMN_KEYS}

        result[table["name"]] = columns

    return result


def _snapshot_tables_v2(snapshot):
    """Validate v2 action evidence without authorizing an executable migration."""
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"format", "dialect", "tables"}
        or snapshot["format"] != _FORMAT_V2
    ):
        raise ValueError("Invalid v2 schema format")
    v1_copy = {**snapshot, "format": _FORMAT, "tables": []}
    for table in snapshot.get("tables", []):
        if (
            not isinstance(table, dict)
            or set(table) != {"name", "columns", "create_sql_preview"}
            or not isinstance(table["columns"], list)
        ):
            raise ValueError("Invalid v2 table")
        if not isinstance(table["create_sql_preview"], str):
            raise ValueError("Invalid v2 SQL preview")
        columns = []
        for column in table["columns"]:
            if not isinstance(column, dict) or set(column) != _COLUMN_KEYS | {
                "on_update",
                "on_delete",
            }:
                raise ValueError("Invalid v2 column evidence")
            ref = column["references"]
            if ref is not None:
                from .models import _identifier

                if not isinstance(ref, str) or ref.count(".") != 1:
                    raise ValueError("Invalid v2 physical foreign-key target")
                for identifier in ref.split("."):
                    _identifier(identifier)
            for action in ("on_update", "on_delete"):
                value = column[action]
                if (value is None) != (ref is None):
                    raise ValueError("Incomplete v2 foreign-key action evidence")
                if value is not None and value not in (
                    "NO ACTION",
                    "RESTRICT",
                    "CASCADE",
                    "SET NULL",
                    "SET DEFAULT",
                ):
                    raise ValueError("Unsupported v2 foreign-key action")
            columns.append({key: column[key] for key in _COLUMN_KEYS})
        v1_copy["tables"].append({**table, "columns": columns})
    _snapshot_tables(v1_copy)
    return {
        table["name"]: {field["name"]: field for field in table["columns"]}
        for table in snapshot["tables"]
    }


def compare_snapshots(before, after):
    """Report declaration differences; never infer renames or execute DDL.

    Every change requires human review. Risk labels prioritize review but do
    not authorize applying changes, even when labelled 'additive'.
    """
    if (
        not isinstance(before, dict)
        or not isinstance(after, dict)
        or before.get("format") != after.get("format")
    ):
        raise ValueError("Cross-version schema comparisons require explicit migration review")
    reader = _snapshot_tables_v2 if before.get("format") == _FORMAT_V2 else _snapshot_tables
    old = reader(before)
    new = reader(after)

    if before["dialect"] != after["dialect"]:
        raise ValueError("Cannot compare snapshots from different dialects")

    changes = []

    for table in sorted(old.keys() | new.keys()):
        if table not in old:
            changes.append(
                {
                    "kind": "added_table",
                    "table": table,
                    "column": None,
                    "risk": "additive",
                    "requires_review": True,
                }
            )
            continue

        if table not in new:
            changes.append(
                {
                    "kind": "removed_table",
                    "table": table,
                    "column": None,
                    "risk": "potential_data_loss",
                    "requires_review": True,
                }
            )
            continue

        for column in sorted(old[table].keys() | new[table].keys()):
            if column not in old[table]:
                field = new[table][column]
                kind = "added_column"

                # NOT NULL without a server default may be impossible to add
                # safely to a populated table; Python defaults do not count.
                risk = (
                    "additive"
                    if field["nullable"] or field["server_default"] is not None
                    else "requires_data_strategy"
                )
            elif column not in new[table]:
                kind, risk = "removed_column", "potential_data_loss"
            elif old[table][column] != new[table][column]:
                kind, risk = "changed_column", "requires_data_strategy"
            else:
                continue

            changes.append(
                {
                    "kind": kind,
                    "table": table,
                    "column": column,
                    "risk": risk,
                    "requires_review": True,
                }
            )

    return changes


# Physical schema record storage and identity resolution.
_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")


def snapshot_directory(project_root, database_name):
    """Use __migrations__ for new histories; retain existing legacy witnesses.

    Historical files require an independently reviewed adoption before moving.
    Neither this resolver nor a read-only history check creates directories.
    """
    if (
        not isinstance(database_name, str)
        or not _SAFE_NAME.fullmatch(database_name)
        or database_name in (".", "..")
    ):
        raise ValueError("Database name cannot safely identify a snapshot directory")

    root = Path(project_root)
    if not root.is_dir() or root.is_symlink():
        raise ValueError("Project directory must be an ordinary directory")

    current_root = root / "__migrations__"
    legacy_root = root / "migrations"
    current = current_root / database_name
    legacy = legacy_root / database_name

    for location in (current_root, legacy_root, current, legacy):
        if location.is_symlink() or (location.exists() and not location.is_dir()):
            raise RuntimeError("Migration snapshot directory is unsafe")

    if current.is_dir() and legacy.is_dir():
        raise RuntimeError(
            "Both legacy and __migrations__ snapshot directories exist for this "
            "database; manual history reconciliation required"
        )

    return legacy if legacy.is_dir() else current


def sqlite_history_name(project_root, database_file):
    """Resolve a configured SQLite identity without changing existing evidence.

    An optional model manifest assigns models to this name; it does not define
    database identity. Direct/non-project callers retain the physical stem.
    """
    import json
    from urllib.parse import unquote, urlsplit

    root = Path(project_root)
    manifest = root / "aurora_databases.json"
    physical = Path(database_file).resolve()
    if not manifest.is_file():
        return physical.stem
    if manifest.is_symlink():
        raise RuntimeError("Database configuration must not be a symbolic link")

    from .configuration import DatabaseConfiguration

    try:
        config = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or set(config) != {"databases"}:
            raise ValueError("Invalid database configuration structure")
        databases = DatabaseConfiguration(config["databases"])
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("Cannot verify configured SQLite history identity") from exc

    matches = []
    for name in databases.names():
        parsed = urlsplit(databases.resolve(name))
        if parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
            continue
        path = unquote(parsed.path)
        if path in ("/:memory:", "//:memory:"):
            continue
        candidate = Path(path[1:]) if path.startswith("//") else root / path.lstrip("/")
        if candidate.resolve() == physical:
            matches.append(name)

    if len(matches) != 1:
        raise RuntimeError("SQLite history identity is not uniquely configured")
    selected = matches[0]

    # Keep a verified old physical-stem namespace in place. Never merge it
    # with a newly configured-name namespace or silently relocate its files.
    if selected != physical.stem:
        legacy_present = any(
            (root / parent / physical.stem).exists() or (root / parent / physical.stem).is_symlink()
            for parent in ("migrations", "__migrations__")
        )
        named_present = any(
            (root / parent / selected).exists() or (root / parent / selected).is_symlink()
            for parent in ("migrations", "__migrations__")
        )
        if legacy_present and named_present:
            raise RuntimeError(
                "Conflicting SQLite snapshot identities; manual history reconciliation required"
            )
        if legacy_present:
            return physical.stem
    return selected


def sqlite_snapshot_directory(project_root, database_file):
    return snapshot_directory(project_root, sqlite_history_name(project_root, database_file))


def _record_directory(project_root, database_file):
    """Select a physical-database witness directory without trusting aliases."""
    return sqlite_snapshot_directory(project_root, database_file)


def prepare_schema_record(project_root, database_file, models):
    """Check that a schema record can be written before approving database DDL."""
    directory = _record_directory(project_root, database_file)
    snapshot = (
        schema_snapshot(*models, dialect="sqlite", allow_v2=True) if models else {"tables": []}
    )

    if not directory.parent.parent.is_dir():
        raise ValueError("Project directory is missing")

    return directory, snapshot


def write_schema_record(project_root, database_file, models, title):
    """Write one complete schema record after committed SQLite DDL.

    A failed record write raises explicitly: the caller must not claim that the
    database operation was rolled back or retry the DDL automatically.
    """
    directory, snapshot = prepare_schema_record(project_root, database_file, models)
    database = Path(database_file)

    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
        physical = [
            {"type": kind, "name": name, "table": table, "sql": sql}
            for kind, name, table, sql in rows
            if not name.startswith("aurorasql_")
        ]
        if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity check failed; schema record not written")

    model_tables = {table["name"] for table in snapshot["tables"]}
    physical_tables = {row["name"] for row in physical if row["type"] == "table"}

    if not model_tables.issubset(physical_tables):
        raise RuntimeError("Database tables differ from model schema; snapshot refused")

    content = {
        "database": sqlite_history_name(project_root, database),
        "engine": "sqlite",
        "title": title,
        "models": snapshot["tables"],
        "physical_schema": physical,
    }
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(content, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    for sequence in range(1000):
        suffix = "" if sequence == 0 else f"-{sequence}"
        candidate = directory / f"{timestamp}{suffix}.json"

        try:
            with candidate.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
            return candidate
        except FileExistsError:
            continue
        except OSError as error:
            # The exclusive-create path belongs to this attempt; never leave
            # a truncated record that could later be mistaken for evidence.
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

            raise RuntimeError(
                "Database operation may have succeeded, but its schema snapshot "
                "could not be written. Inspect database history before retrying."
            ) from error

    raise RuntimeError("No available timestamped schema snapshot filename")
