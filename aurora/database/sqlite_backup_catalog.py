"""SQLite backup catalog. Manifests remain the recovery authority.

Old eight-column catalogs can be inspected but are never rewritten implicitly.
"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .backup_artifacts import _root, list_artifacts

_COLUMNS = ("id", "title", "artifact", "date", "warnings")
_DDL = ('CREATE TABLE "__backups__" ('
        '"id" INTEGER PRIMARY KEY AUTOINCREMENT, "title" TEXT, '
        '"artifact" TEXT NOT NULL UNIQUE, "date" TEXT NOT NULL, '
        '"warnings" TEXT)')
_LEGACY = ("id", "title", "completed_at", "backend", "artifact",
           "artifact_sha256", "destination_sha256", "manifest_sha256")
_LEGACY_DDL = ('CREATE TABLE "__backups__" ('
               '"id" TEXT PRIMARY KEY, "title" TEXT, "completed_at" TEXT NOT NULL, '
               '"backend" TEXT NOT NULL, "artifact" TEXT NOT NULL, '
               '"artifact_sha256" TEXT NOT NULL, "destination_sha256" TEXT NOT NULL, '
               '"manifest_sha256" TEXT NOT NULL)')


def _catalog_format(db):
    found = db.execute("SELECT type, sql FROM sqlite_master WHERE name='__backups__'").fetchall()
    if not found:
        return None
    if len(found) != 1 or found[0][0] != "table":
        raise ValueError("Conflicting __backups__ database object; manual review required")
    columns = db.execute('PRAGMA table_info("__backups__")').fetchall()
    names = tuple(row[1] for row in columns)
    if names == _LEGACY and found[0][1] == _LEGACY_DDL and columns[0][5] == 1:
        return "legacy"
    if (names != _COLUMNS or found[0][1] != _DDL or columns[0][5] != 1
            or any(row[5] for row in columns[1:])):
        raise ValueError("Conflicting __backups__ table definition; manual review required")
    return "current"


def _catalog_exists(db):
    return _catalog_format(db) is not None


def _manifest_rows(project_root, name, identity):
    reports = list_artifacts(project_root, name, identity)
    if any(item["status"] != "ARTIFACT_VERIFIED" for item in reports):
        raise ValueError("Backup manifest inventory needs manual reconciliation")
    folder = _root(project_root, name)
    rows = {}
    for item in reports:
        manifest = json.loads((folder / (item["id"] + ".json")).read_text(encoding="utf-8"))
        if manifest.get("backend") != "sqlite" or manifest.get("destination_sha256") != identity:
            raise ValueError("Backup catalog requires verified SQLite artifact ownership")
        rows[manifest["artifact"]] = (manifest.get("title"), manifest["artifact"],
                                      manifest.get("date", manifest.get("completed_at")), None)
    return rows


def _read_rows(db, kind):
    if kind == "legacy":
        return {row[0]: tuple(row) for row in db.execute(
            'SELECT id,title,completed_at,backend,artifact,artifact_sha256,'
            'destination_sha256,manifest_sha256 FROM "__backups__"')}
    return {row[2]: tuple(row) for row in db.execute(
        'SELECT id,title,artifact,date,warnings FROM "__backups__"')}


def reconcile_sqlite_catalog(database_file, project_root, name, identity):
    """Create/backfill only current-format catalog; legacy requires reviewed transition."""
    rows = _manifest_rows(project_root, name, identity)
    path = Path(database_file)
    if not path.is_file() or path.is_symlink():
        raise ValueError("SQLite catalog requires an existing regular database file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            kind = _catalog_format(db)
            if kind == "legacy":
                raise ValueError("Legacy __backups__ catalog requires an explicitly reviewed transition")
            existing = _read_rows(db, kind) if kind else {}
            if any(filename not in rows or tuple(value[1:]) != rows[filename]
                   for filename, value in existing.items()):
                raise ValueError("Backup catalog conflicts with artifact manifests")
            if kind is None:
                db.execute(_DDL)
            for filename, row in rows.items():
                if filename not in existing:
                    db.execute('INSERT INTO "__backups__" (title,artifact,date,warnings) '
                               'VALUES (?,?,?,?)', row)
            db.commit()
        except BaseException:
            db.rollback()
            raise


def inspect_sqlite_catalog(database_file, project_root, name, identity, reports):
    path = Path(database_file)
    if not path.is_file() or path.is_symlink():
        return reports
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        kind = _catalog_format(db)
        if kind is None:
            return reports
        existing = _read_rows(db, kind)
    try:
        expected = _manifest_rows(project_root, name, identity)
    except (ValueError, OSError, KeyError, json.JSONDecodeError):
        return reports
    if kind == "legacy":
        # Old full-evidence catalogs remain subject to their original checks.
        import hashlib
        folder = _root(project_root, name)
        for report in reports:
            if report["status"] != "ARTIFACT_VERIFIED":
                continue
            ident = report["id"]
            manifest_file = folder / (ident + ".json")
            raw = manifest_file.read_bytes()
            item = json.loads(raw)
            original = (ident, item.get("title"), item.get("date", item.get("completed_at")), item["backend"],
                        item["artifact"], item["sha256"], identity,
                        hashlib.sha256(raw).hexdigest())
            if ident in existing and existing[ident] != original:
                report["status"] = "CATALOG_MISMATCH"
        for ident in existing.keys() - {r["id"] for r in reports}:
            reports.append({"id": ident, "status": "CATALOG_ORPHAN"})
    else:
        for report in reports:
            if report["status"] == "ARTIFACT_VERIFIED":
                artifact = next((filename for filename in expected if
                                 filename.rsplit(".", 1)[0] == report["id"]), None)
                if artifact in existing and tuple(existing[artifact][1:]) != expected[artifact]:
                    report["status"] = "CATALOG_MISMATCH"
        for filename in existing.keys() - expected.keys():
            reports.append({"id": filename, "status": "CATALOG_ORPHAN"})
    return reports
