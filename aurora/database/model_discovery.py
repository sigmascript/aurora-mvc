"""Discover conventionally located project models with optional database assignment."""

import keyword
from pathlib import Path
import re

from .model_manifest import read_model_mapping


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CLASS = re.compile(r"[A-Z][A-Za-z0-9]*\Z")


def discover_model_refs(project_root, manifest, database="default"):
    """Discover named model files, then apply explicit alias assignments.

    Never traverse arbitrary directories or import modules during discovery.
    Model class validation is performed by the existing loader afterward.
    """
    root = Path(project_root)
    mapping = read_model_mapping(manifest)
    assignments = {ref: alias for alias, refs in mapping.items() for ref in refs}
    found = set()
    folders = [(root / "models", "models"), (root / "app" / "models", "app.models")]
    apps = root / "apps"

    if apps.is_dir() and not apps.is_symlink():
        for app in sorted(apps.iterdir()):
            if app.is_dir() and not app.is_symlink() and _NAME.fullmatch(app.name):
                folders.append((app / "models", f"apps.{app.name}.models"))

    for folder, module in folders:
        if not folder.is_dir() or folder.is_symlink():
            continue

        for file in sorted(folder.glob("*.py")):
            if (file.is_symlink() or not file.is_file() or
                    not _CLASS.fullmatch(file.stem) or keyword.iskeyword(file.stem)):
                continue

            ref = f"{module}.{file.stem}:{file.stem}"

            if assignments.get(ref, "default") == database:
                found.add(ref)

    return tuple(sorted(found | set(mapping.get(database, ()))))
