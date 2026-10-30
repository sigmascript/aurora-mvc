"""Deterministic, read-only model-schema descriptions for human review.

Snapshots are JSON-compatible descriptions, not executable migrations.
No live database is inspected and Python defaults are never invoked.
"""

from datetime import date, datetime

from .schema import create_table_sql


_FORMAT = "aurorasql.schema.v1"
_DIALECTS = ("sqlite", "mysql", "postgresql")
_COLUMN_KEYS = frozenset({
    "name", "type", "length", "primary_key", "nullable", "unique",
    "references", "python_default", "server_default",
})


def _default_description(value):
    if callable(value):
        return {"kind": "callable", "name": f"{value.__module__}.{value.__qualname__}"}
    if isinstance(value, (date, datetime)):
        return {"kind": "literal", "value": value.isoformat()}
    if value is None or type(value) in (str, int, float, bool):
        return {"kind": "literal", "value": value}
    raise TypeError("Unsupported model default for snapshot")


def schema_snapshot(*models, dialect):
    """Describe model declarations in stable order, without database access."""
    if not models:
        raise ValueError("At least one model is required")
    tables = []
    names = set()
    for model in models:
        ddl = create_table_sql(model, dialect)
        name = model.__table_name__
        if name in names:
            raise ValueError(f"Duplicate model table: {name}")
        names.add(name)
        columns = []
        for field_name, field in sorted(model.__columns__.items()):
            columns.append({
                "name": field_name,
                "type": field.python_type.__name__,
                "length": field.length,
                "primary_key": field.primary_key,
                "nullable": field.nullable,
                "unique": field.unique,
                "references": field.references,
                "python_default": _default_description(field.default),
                "server_default": field.server_default,
            })
        tables.append({"name": name, "columns": columns, "create_sql_preview": ddl})
    return {"format": _FORMAT, "dialect": dialect,
            "tables": sorted(tables, key=lambda table: table["name"])}


def _snapshot_tables(snapshot):
    """Validate the public v1 shape before comparing any declarations."""
    if not isinstance(snapshot, dict) or snapshot.get("format") != _FORMAT:
        raise ValueError("Unsupported schema snapshot format")
    if snapshot.get("dialect") not in _DIALECTS:
        raise ValueError("Unsupported schema snapshot dialect")
    if not isinstance(snapshot.get("tables"), list):
        raise ValueError("Invalid snapshot tables")
    result = {}
    for table in snapshot["tables"]:
        if not isinstance(table, dict) or not isinstance(table.get("name"), str) or not table["name"]:
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
                raise ValueError("Invalid snapshot column type")
            if field["length"] is not None and (type(field["length"]) is not int or field["length"] <= 0):
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
            elif set(default) != {"kind", "value"} or not (default["value"] is None or type(default["value"]) in (str, int, float, bool)):
                raise ValueError("Invalid snapshot literal default")
            # create_sql_preview is intentionally excluded: it is an advisory
            # rendering, not the canonical schema declaration.
            columns[name] = {key: field[key] for key in _COLUMN_KEYS}
        result[table["name"]] = columns
    return result


def compare_snapshots(before, after):
    """Report declaration differences; never infer renames or execute DDL.

    Every change requires human review. Risk labels prioritize review but do
    not authorize applying changes, even when labelled 'additive'.
    """
    old = _snapshot_tables(before)
    new = _snapshot_tables(after)
    if before["dialect"] != after["dialect"]:
        raise ValueError("Cannot compare snapshots from different dialects")
    changes = []
    for table in sorted(old.keys() | new.keys()):
        if table not in old:
            changes.append({"kind": "added_table", "table": table, "column": None,
                            "risk": "additive", "requires_review": True})
            continue
        if table not in new:
            changes.append({"kind": "removed_table", "table": table, "column": None,
                            "risk": "potential_data_loss", "requires_review": True})
            continue
        for column in sorted(old[table].keys() | new[table].keys()):
            if column not in old[table]:
                field = new[table][column]
                kind = "added_column"
                # NOT NULL without a server default may be impossible to add
                # safely to a populated table; Python defaults do not count.
                risk = "additive" if field["nullable"] or field["server_default"] is not None else "requires_data_strategy"
            elif column not in new[table]:
                kind, risk = "removed_column", "potential_data_loss"
            elif old[table][column] != new[table][column]:
                kind, risk = "changed_column", "requires_data_strategy"
            else:
                continue
            changes.append({"kind": kind, "table": table, "column": column,
                            "risk": risk, "requires_review": True})
    return changes
