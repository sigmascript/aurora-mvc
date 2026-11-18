"""Immutable backup artifacts with filesystem manifests independent of the database."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote
import re
from urllib.parse import urlsplit

from .configuration import _destination_identity


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
    if title is not None and (not isinstance(title, str) or len(title) > 200 or
                              any(ord(char) < 32 for char in title)):
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
        if not output.exists() and not output.is_symlink() and not output.with_suffix(".json").exists():
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
    metadata = {"database": database, "backend": backend, "title": title,
                "artifact": path.name, "date": datetime.now(timezone.utc).isoformat(),
                "bytes": path.stat().st_size, "sha256": _sha256(path),
                "destination_sha256": identity}
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
            if (metadata.get("id", manifest.stem) != manifest.stem
                    or metadata["database"] != database or metadata["artifact"] != filename
                    or not isinstance(metadata.get("date", metadata.get("completed_at")), str)
                    or not isinstance(metadata["bytes"], int) or metadata["bytes"] < 1
                    or not isinstance(metadata["sha256"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", metadata["sha256"])):
                raise ValueError
            artifact = folder / filename
            valid = (identity is None or metadata.get("destination_sha256") == identity) and (
                     artifact.is_file() and not artifact.is_symlink()
                     and artifact.stat().st_size == metadata["bytes"]
                     and _sha256(artifact) == metadata["sha256"])
            entry.update(title=metadata.get("title"), backend=backend,
                         status=("DESTINATION_MISMATCH" if identity is not None and
                                 metadata.get("destination_sha256") != identity else
                                 "ARTIFACT_VERIFIED" if valid else "MISSING_OR_CHANGED"))
            known.add(filename)
        except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
            pass
        reports.append(entry)
    for item in sorted(folder.iterdir()):
        if item.suffix in _FORMATS.values() and item.name not in known:
            reports.append({"id": item.name, "status": "UNRECORDED_ARTIFACT"})
    return reports
