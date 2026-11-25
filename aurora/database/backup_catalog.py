"""Immutable backup artifacts with filesystem manifests independent of the database."""

import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .configuration import _destination_identity
from .schema_inspection import inspect_table

_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_ID = re.compile(r"(?:\d{8}-\d{6}(?:-[1-9][0-9]*)?|\d{8}T\d{6}Z-[0-9a-f]{24})\Z")
_FORMATS = {"sqlite": ".sqlite3", "mysql": ".sql", "postgresql": ".dump"}


def _root(project_root, database, *, create=False):
    if not _NAME.fullmatch(database):
        raise ValueError("Invalid configured database name for backup storage")
    root = Path(project_root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Backup project root must be a directory")
    parent = root / "__backups__"
    folder = parent / database
    for path in (parent, folder):
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError("Backup storage path is unsafe")
    if create:
        parent.mkdir(exist_ok=True)
        folder.mkdir(exist_ok=True)
    return folder


def destination_fingerprint(url, project_root=None):
    """Bind artifact ownership to physical destination, without storing credentials."""
    parsed = urlsplit(url)
    if parsed.scheme.startswith("sqlite") and project_root is not None:
        path = unquote(parsed.path)
        if path in ("/:memory:", ""):
            raise ValueError("Backups require a persistent SQLite destination")
        value = path[2:] if path.startswith("///") else path[1:]
        physical = Path(value)
        if not physical.is_absolute():
            physical = Path(project_root) / physical
        identity = ("sqlite", os.path.normcase(str(physical.resolve(strict=False))))
    else:
        identity = _destination_identity(parsed)
    if identity is None:
        raise ValueError("Backups require a persistent named database destination")
    return hashlib.sha256(repr(identity).encode("utf-8")).hexdigest()


def validate_title(title):
    if title is not None and (
        not isinstance(title, str) or len(title) > 200 or any(ord(char) < 32 for char in title)
    ):
        raise ValueError("Backup title must be printable text up to 200 characters")


def prepare_artifact(project_root, database, backend):
    """Reserve no names: backend publisher must use exclusive creation."""
    folder = _root(project_root, database, create=True)
    if backend not in _FORMATS:
        raise ValueError("Unsupported backup backend")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for index in range(10000):
        backup_id = stamp + (f"-{index}" if index else "")
        output = folder / (backup_id + _FORMATS[backend])
        if (
            not output.exists()
            and not output.is_symlink()
            and not output.with_suffix(".json").exists()
        ):
            return backup_id, output
    raise FileExistsError("Unable to allocate unique backup identifier")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def record_artifact(project_root, database, backend, backup_id, path, title, identity):
    """Publish one immutable manifest only after complete backend verification."""
    validate_title(title)
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        raise ValueError("Backup destination identity is unverified")
    folder = _root(project_root, database)
    path = Path(path)
    if not _ID.fullmatch(backup_id) or path != folder / (backup_id + _FORMATS[backend]):
        raise ValueError("Backup artifact identity does not match selected database")
    if path.is_symlink() or not path.is_file():
        raise ValueError("Verified backup artifact is missing or unsafe")
    metadata = {
        "database": database,
        "backend": backend,
        "title": title,
        "artifact": path.name,
        "date": datetime.now(timezone.utc).isoformat(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "destination_sha256": identity,
    }
    manifest = folder / (backup_id + ".json")
    fd = os.open(manifest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(metadata, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        manifest.unlink(missing_ok=True)
        raise
    return metadata


def list_artifacts(project_root, database, identity=None):
    """Read only: never create, repair, delete or silently trust a manifest."""
    folder = _root(project_root, database)
    if not folder.exists():
        return []
    reports = []
    known = set()
    for manifest in sorted(folder.glob("*.json")):
        valid = False
        entry = {"id": manifest.stem, "status": "INVALID"}
        try:
            if manifest.is_symlink() or not _ID.fullmatch(manifest.stem):
                raise ValueError
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            backend = metadata["backend"]
            extension = _FORMATS[backend]
            filename = manifest.stem + extension
            if (
                metadata.get("id", manifest.stem) != manifest.stem
                or metadata["database"] != database
                or metadata["artifact"] != filename
                or not isinstance(metadata.get("date", metadata.get("completed_at")), str)
                or not isinstance(metadata["bytes"], int)
                or metadata["bytes"] < 1
                or not isinstance(metadata["sha256"], str)
                or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])
            ):
                raise ValueError
            artifact = folder / filename
            valid = (identity is None or metadata.get("destination_sha256") == identity) and (
                artifact.is_file()
                and not artifact.is_symlink()
                and artifact.stat().st_size == metadata["bytes"]
                and _sha256(artifact) == metadata["sha256"]
            )
            entry.update(
                title=metadata.get("title"),
                backend=backend,
                status=(
                    "DESTINATION_MISMATCH"
                    if identity is not None and metadata.get("destination_sha256") != identity
                    else "ARTIFACT_VERIFIED"
                    if valid
                    else "MISSING_OR_CHANGED"
                ),
            )
            known.add(filename)
        except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
            pass
        reports.append(entry)
    for item in sorted(folder.iterdir()):
        if item.suffix in _FORMATS.values() and item.name not in known:
            reports.append({"id": item.name, "status": "UNRECORDED_ARTIFACT"})
    return reports


# MySQL/PostgreSQL backup catalog: derived evidence only.

_COLUMNS = ("id", "title", "artifact", "date", "warnings")
_LEGACY = (
    "id",
    "title",
    "completed_at",
    "backend",
    "artifact",
    "artifact_sha256",
    "destination_sha256",
    "manifest_sha256",
)
_DDL_MYSQL = (
    "CREATE TABLE __backups__ ("
    "id BIGINT AUTO_INCREMENT PRIMARY KEY, title TEXT NULL, "
    "artifact VARCHAR(180) NOT NULL UNIQUE, date VARCHAR(64) NOT NULL, "
    "warnings TEXT NULL)"
)
_DDL_PG = (
    "CREATE TABLE __backups__ ("
    "id BIGSERIAL PRIMARY KEY, title TEXT NULL, "
    "artifact VARCHAR(180) NOT NULL UNIQUE, date VARCHAR(64) NOT NULL, "
    "warnings TEXT NULL)"
)
_SELECT = "SELECT id,title,artifact,date,warnings FROM __backups__"
_LEGACY_SELECT = (
    "SELECT id,title,completed_at,backend,artifact,artifact_sha256,"
    "destination_sha256,manifest_sha256 FROM __backups__"
)
_INSERT = (
    "INSERT INTO __backups__ (title,artifact,date,warnings) "
    "VALUES (:title,:artifact,:date,:warnings)"
)


def _catalog_format(connection):
    table = inspect_table(connection, "__backups__")
    if not table.exists:
        from .migration_history import _objects

        if "__backups__" in _objects(connection):
            raise ValueError("Conflicting __backups__ database object")
        return None
    columns = table.columns
    names = tuple(col.name for col in columns)

    def norm(tp):
        return tp.lower().replace("character varying", "varchar").replace(" ", "")

    types = tuple(norm(col.sql_type) for col in columns)
    if (
        names == _LEGACY
        and types
        == (
            "varchar(80)",
            "text",
            "varchar(64)",
            "varchar(16)",
            "varchar(180)",
            "varchar(64)",
            "varchar(64)",
            "varchar(64)",
        )
        and columns[0].primary_key
        and not columns[0].nullable
        and columns[1].nullable
        and all(not col.nullable for col in columns[2:])
        and not any(col.primary_key for col in columns[1:])
        and not table.foreign_keys
        and table.uniqueness_verified
        and len(table.indexes) == 1
    ):
        return "legacy"
    if (
        names != _COLUMNS
        or types != ("bigint", "text", "varchar(180)", "varchar(64)", "text")
        or not columns[0].primary_key
        or columns[0].nullable
        or columns[1].nullable is False
        or columns[2].nullable
        or columns[3].nullable
        or columns[4].nullable is False
        or any(col.primary_key for col in columns[1:])
        or table.foreign_keys
        or not table.uniqueness_verified
        or len(table.indexes) != 2
    ):
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
        records[item["artifact"]] = dict(
            title=item.get("title"),
            artifact=item["artifact"],
            date=item.get("date", item.get("completed_at")),
            warnings=None,
        )
    return records


def _read_rows(connection, kind="current"):
    return {
        item["artifact"]: item
        for item in connection.fetch_all(_LEGACY_SELECT if kind == "legacy" else _SELECT)
    }


def _compare(current, expected):
    for filename, row in current.items():
        if filename not in expected or any(
            row[k] != expected[filename][k] for k in expected[filename]
        ):
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
            original = dict(
                id=ident,
                title=item.get("title"),
                completed_at=item.get("date", item.get("completed_at")),
                backend=item["backend"],
                artifact=item["artifact"],
                artifact_sha256=item["sha256"],
                destination_sha256=identity,
                manifest_sha256=hashlib.sha256(raw).hexdigest(),
            )
            if item["artifact"] in existing and existing[item["artifact"]] != original:
                report["status"] = "CATALOG_MISMATCH"
    else:
        for report in reports:
            if report["status"] == "ARTIFACT_VERIFIED":
                candidate = next((f for f in expected if f.rsplit(".", 1)[0] == report["id"]), None)
                if candidate in existing and any(
                    existing[candidate][k] != expected[candidate][k] for k in expected[candidate]
                ):
                    report["status"] = "CATALOG_MISMATCH"
    for filename in existing.keys() - expected.keys():
        reports.append({"id": filename, "status": "CATALOG_ORPHAN"})
    return reports


def reconcile_server_catalog(connection, root, name, identity):
    """Only new-format catalogs can be created/backfilled by an explicit backup."""
    from .migration_coordination import migration_lock

    if connection.dialect not in ("mysql", "postgresql"):
        raise ValueError("Server backup catalog needs MySQL or PostgreSQL")
    expected = _manifest_rows(root, name, identity, connection.dialect)
    started = False

    def update_locked():
        nonlocal started
        kind = _catalog_format(connection)
        if kind == "legacy":
            raise ValueError(
                "Legacy __backups__ catalog requires an explicitly reviewed transition"
            )
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
        from .migration_coordination import _PG_LOCK_KEY

        with connection.transaction():
            row = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not row or row["acquired"] is not True:
                raise RuntimeError("PostgreSQL backup catalog lock unavailable")
            update_locked()
    else:
        with migration_lock(connection):
            try:
                update_locked()
            except Exception as exc:
                if started:
                    raise RuntimeError(
                        "MySQL backup catalog outcome uncertain; inspect table and manifests"
                    ) from exc
                raise


_SQLITE_COLUMNS = ("id", "title", "artifact", "date", "warnings")
_SQLITE_DDL = (
    'CREATE TABLE "__backups__" ('
    '"id" INTEGER PRIMARY KEY AUTOINCREMENT, "title" TEXT, '
    '"artifact" TEXT NOT NULL UNIQUE, "date" TEXT NOT NULL, '
    '"warnings" TEXT)'
)
_SQLITE_LEGACY = (
    "id",
    "title",
    "completed_at",
    "backend",
    "artifact",
    "artifact_sha256",
    "destination_sha256",
    "manifest_sha256",
)
_SQLITE_LEGACY_DDL = (
    'CREATE TABLE "__backups__" ('
    '"id" TEXT PRIMARY KEY, "title" TEXT, "completed_at" TEXT NOT NULL, '
    '"backend" TEXT NOT NULL, "artifact" TEXT NOT NULL, '
    '"artifact_sha256" TEXT NOT NULL, "destination_sha256" TEXT NOT NULL, '
    '"manifest_sha256" TEXT NOT NULL)'
)


# SQLite backup catalog is derived from verified immutable artifact manifests.


def _sqlite_catalog_format(db):
    found = db.execute("SELECT type, sql FROM sqlite_master WHERE name='__backups__'").fetchall()
    if not found:
        return None
    if len(found) != 1 or found[0][0] != "table":
        raise ValueError("Conflicting __backups__ database object; manual review required")
    columns = db.execute('PRAGMA table_info("__backups__")').fetchall()
    names = tuple(row[1] for row in columns)
    if names == _SQLITE_LEGACY and found[0][1] == _SQLITE_LEGACY_DDL and columns[0][5] == 1:
        return "legacy"
    if (
        names != _SQLITE_COLUMNS
        or found[0][1] != _SQLITE_DDL
        or columns[0][5] != 1
        or any(row[5] for row in columns[1:])
    ):
        raise ValueError("Conflicting __backups__ table definition; manual review required")
    return "current"


def _sqlite_catalog_exists(db):
    return _sqlite_catalog_format(db) is not None


def _sqlite_manifest_rows(project_root, name, identity):
    reports = list_artifacts(project_root, name, identity)
    if any(item["status"] != "ARTIFACT_VERIFIED" for item in reports):
        raise ValueError("Backup manifest inventory needs manual reconciliation")
    folder = _root(project_root, name)
    rows = {}
    for item in reports:
        manifest = json.loads((folder / (item["id"] + ".json")).read_text(encoding="utf-8"))
        if manifest.get("backend") != "sqlite" or manifest.get("destination_sha256") != identity:
            raise ValueError("Backup catalog requires verified SQLite artifact ownership")
        rows[manifest["artifact"]] = (
            manifest.get("title"),
            manifest["artifact"],
            manifest.get("date", manifest.get("completed_at")),
            None,
        )
    return rows


def _sqlite_read_rows(db, kind):
    if kind == "legacy":
        return {
            row[0]: tuple(row)
            for row in db.execute(
                "SELECT id,title,completed_at,backend,artifact,artifact_sha256,"
                'destination_sha256,manifest_sha256 FROM "__backups__"'
            )
        }
    return {
        row[2]: tuple(row)
        for row in db.execute('SELECT id,title,artifact,date,warnings FROM "__backups__"')
    }


def reconcile_sqlite_catalog(database_file, project_root, name, identity):
    """Create/backfill only current-format catalog; legacy requires reviewed transition."""
    rows = _sqlite_manifest_rows(project_root, name, identity)
    path = Path(database_file)
    if not path.is_file() or path.is_symlink():
        raise ValueError("SQLite catalog requires an existing regular database file")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            kind = _sqlite_catalog_format(db)
            if kind == "legacy":
                raise ValueError(
                    "Legacy __backups__ catalog requires an explicitly reviewed transition"
                )
            existing = _sqlite_read_rows(db, kind) if kind else {}
            if any(
                filename not in rows or tuple(value[1:]) != rows[filename]
                for filename, value in existing.items()
            ):
                raise ValueError("Backup catalog conflicts with artifact manifests")
            if kind is None:
                db.execute(_SQLITE_DDL)
            for filename, row in rows.items():
                if filename not in existing:
                    db.execute(
                        'INSERT INTO "__backups__" (title,artifact,date,warnings) VALUES (?,?,?,?)',
                        row,
                    )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def inspect_sqlite_catalog(database_file, project_root, name, identity, reports):
    path = Path(database_file)
    if not path.is_file() or path.is_symlink():
        return reports
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        kind = _sqlite_catalog_format(db)
        if kind is None:
            return reports
        existing = _sqlite_read_rows(db, kind)
    try:
        expected = _sqlite_manifest_rows(project_root, name, identity)
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
            original = (
                ident,
                item.get("title"),
                item.get("date", item.get("completed_at")),
                item["backend"],
                item["artifact"],
                item["sha256"],
                identity,
                hashlib.sha256(raw).hexdigest(),
            )
            if ident in existing and existing[ident] != original:
                report["status"] = "CATALOG_MISMATCH"
        for ident in existing.keys() - {r["id"] for r in reports}:
            reports.append({"id": ident, "status": "CATALOG_ORPHAN"})
    else:
        for report in reports:
            if report["status"] == "ARTIFACT_VERIFIED":
                artifact = next(
                    (
                        filename
                        for filename in expected
                        if filename.rsplit(".", 1)[0] == report["id"]
                    ),
                    None,
                )
                if artifact in existing and tuple(existing[artifact][1:]) != expected[artifact]:
                    report["status"] = "CATALOG_MISMATCH"
        for filename in existing.keys() - expected.keys():
            reports.append({"id": filename, "status": "CATALOG_ORPHAN"})
    return reports
