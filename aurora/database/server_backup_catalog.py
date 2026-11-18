"""Derived server backup history. Manifests are the independent authority.

Existing eight-column catalogs remain readable, never auto-replaced.
"""

import hashlib
import json

from .backup_artifacts import _root, list_artifacts
from .inspection import inspect_table

_COLUMNS = ("id", "title", "artifact", "date", "warnings")
_LEGACY = ("id", "title", "completed_at", "backend", "artifact",
           "artifact_sha256", "destination_sha256", "manifest_sha256")
_DDL_MYSQL = ('CREATE TABLE __backups__ ('
              'id BIGINT AUTO_INCREMENT PRIMARY KEY, title TEXT NULL, '
              'artifact VARCHAR(180) NOT NULL UNIQUE, date VARCHAR(64) NOT NULL, '
              'warnings TEXT NULL)')
_DDL_PG = ('CREATE TABLE __backups__ ('
           'id BIGSERIAL PRIMARY KEY, title TEXT NULL, '
           'artifact VARCHAR(180) NOT NULL UNIQUE, date VARCHAR(64) NOT NULL, '
           'warnings TEXT NULL)')
_SELECT = 'SELECT id,title,artifact,date,warnings FROM __backups__'
_LEGACY_SELECT = ('SELECT id,title,completed_at,backend,artifact,artifact_sha256,'
                  'destination_sha256,manifest_sha256 FROM __backups__')
_INSERT = ('INSERT INTO __backups__ (title,artifact,date,warnings) '
           'VALUES (:title,:artifact,:date,:warnings)')


def _catalog_format(connection):
    table = inspect_table(connection, "__backups__")
    if not table.exists:
        from .server_forward_history import _objects
        if "__backups__" in _objects(connection):
            raise ValueError("Conflicting __backups__ database object")
        return None
    columns = table.columns
    names = tuple(col.name for col in columns)
    def norm(tp):
        return tp.lower().replace("character varying", "varchar").replace(" ", "")
    types = tuple(norm(col.sql_type) for col in columns)
    if (names == _LEGACY and types == ("varchar(80)", "text", "varchar(64)",
                                     "varchar(16)", "varchar(180)", "varchar(64)",
                                     "varchar(64)", "varchar(64)")
            and columns[0].primary_key and not columns[0].nullable
            and columns[1].nullable and all(not col.nullable for col in columns[2:])
            and not any(col.primary_key for col in columns[1:])
            and not table.foreign_keys and table.uniqueness_verified and len(table.indexes) == 1):
        return "legacy"
    if (names != _COLUMNS or types != ("bigint", "text", "varchar(180)",
                                       "varchar(64)", "text")
            or not columns[0].primary_key or columns[0].nullable
            or columns[1].nullable is False or columns[2].nullable
            or columns[3].nullable or columns[4].nullable is False
            or any(col.primary_key for col in columns[1:])
            or table.foreign_keys or not table.uniqueness_verified
            or len(table.indexes) != 2):
        raise ValueError("Conflicting __backups__ table definition")
    return "current"


def _catalog_exists(connection):
    return _catalog_format(connection) is not None


def _manifest_rows(root, name, identity, backend):
    reports = list_artifacts(root, name, identity)
    if any(item["status"] != "ARTIFACT_VERIFIED" for item in reports):
        raise ValueError("Backup manifests require manual reconciliation")
    folder = _root(root, name)
    records = {}
    for report in reports:
        item = json.loads((folder / (report["id"] + ".json")).read_text(encoding="utf-8"))
        if item.get("backend") != backend or item.get("destination_sha256") != identity:
            raise ValueError("Backup artifact ownership does not match selected database")
        records[item["artifact"]] = dict(title=item.get("title"), artifact=item["artifact"],
                                         date=item.get("date", item.get("completed_at")), warnings=None)
    return records


def _read_rows(connection, kind="current"):
    return {item["artifact"]: item for item in connection.fetch_all(
        _LEGACY_SELECT if kind == "legacy" else _SELECT)}


def _compare(current, expected):
    for filename, row in current.items():
        if filename not in expected or any(row[k] != expected[filename][k] for k in expected[filename]):
            raise ValueError("Backup catalog conflicts with verified manifests")


def inspect_server_catalog(connection, root, name, identity, reports):
    kind = _catalog_format(connection)
    if kind is None:
        return reports
    existing = _read_rows(connection, kind)
    try:
        expected = _manifest_rows(root, name, identity, connection.dialect)
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return reports
    if kind == "legacy":
        folder = _root(root, name)
        for report in reports:
            if report["status"] != "ARTIFACT_VERIFIED":
                continue
            ident = report["id"]
            raw = (folder / (ident + ".json")).read_bytes()
            item = json.loads(raw)
            original = dict(id=ident, title=item.get("title"), completed_at=item.get("date", item.get("completed_at")),
                            backend=item["backend"], artifact=item["artifact"],
                            artifact_sha256=item["sha256"], destination_sha256=identity,
                            manifest_sha256=hashlib.sha256(raw).hexdigest())
            if item["artifact"] in existing and existing[item["artifact"]] != original:
                report["status"] = "CATALOG_MISMATCH"
    else:
        for report in reports:
            if report["status"] == "ARTIFACT_VERIFIED":
                candidate = next((f for f in expected if f.rsplit(".", 1)[0] == report["id"]), None)
                if candidate in existing and any(existing[candidate][k] != expected[candidate][k]
                                                 for k in expected[candidate]):
                    report["status"] = "CATALOG_MISMATCH"
    for filename in existing.keys() - expected.keys():
        reports.append({"id": filename, "status": "CATALOG_ORPHAN"})
    return reports


def reconcile_server_catalog(connection, root, name, identity):
    """Only new-format catalogs can be created/backfilled by an explicit backup."""
    from .coordination import migration_lock
    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server backup catalog needs MySQL or PostgreSQL")
    expected = _manifest_rows(root, name, identity, connection.dialect)
    started = False

    def update_locked():
        nonlocal started
        kind = _catalog_format(connection)
        if kind == "legacy":
            raise ValueError("Legacy __backups__ catalog requires an explicitly reviewed transition")
        current = _read_rows(connection) if kind else {}
        _compare(current, expected)
        if kind is None:
            started = True
            connection.execute_write(_DDL_MYSQL if connection.dialect == "mysql" else _DDL_PG)
        for filename, row in expected.items():
            if filename not in current:
                started = True
                connection.execute_write(_INSERT, row)
        _compare(_read_rows(connection), expected)

    if connection.dialect == "postgresql":
        from .coordination import _PG_LOCK_KEY
        with connection.transaction():
            row = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY})
            if not row or row["acquired"] is not True:
                raise RuntimeError("PostgreSQL backup catalog lock unavailable")
            update_locked()
    else:
        with migration_lock(connection):
            try:
                update_locked()
            except Exception as exc:
                if started:
                    raise RuntimeError("MySQL backup catalog outcome uncertain; inspect table and manifests") from exc
                raise
