"""Select one verified snapshot location without relocating existing evidence."""

import re
from pathlib import Path


_SAFE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}\Z")


def snapshot_directory(project_root, database_name):
    """Use __migrations__ for new histories; retain existing legacy witnesses.

    Historical files require an independently reviewed adoption before moving.
    Neither this resolver nor a read-only history check creates directories.
    """
    if (not isinstance(database_name, str) or
            not _SAFE_NAME.fullmatch(database_name) or database_name in (".", "..")):
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
            "database; manual history reconciliation required")

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
        candidate = (Path(path[1:]) if path.startswith("//")
                     else root / path.lstrip("/"))
        if candidate.resolve() == physical:
            matches.append(name)

    if len(matches) != 1:
        raise RuntimeError("SQLite history identity is not uniquely configured")
    selected = matches[0]

    # Keep a verified old physical-stem namespace in place. Never merge it
    # with a newly configured-name namespace or silently relocate its files.
    if selected != physical.stem:
        legacy_present = any((root / parent / physical.stem).exists() or
                             (root / parent / physical.stem).is_symlink()
                             for parent in ("migrations", "__migrations__"))
        named_present = any((root / parent / selected).exists() or
                            (root / parent / selected).is_symlink()
                            for parent in ("migrations", "__migrations__"))
        if legacy_present and named_present:
            raise RuntimeError("Conflicting SQLite snapshot identities; manual history reconciliation required")
        if legacy_present:
            return physical.stem
    return selected


def sqlite_snapshot_directory(project_root, database_file):
    return snapshot_directory(project_root, sqlite_history_name(project_root, database_file))
