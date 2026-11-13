"""Register explicitly generated AuroraSQL models without importing application code."""

import json
import os
import re
import tempfile
from pathlib import Path


_REFERENCE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")


def register_generated_model(destination, name, *, project=None, alias="default", manifest="aurora_models.json"):
    """Atomically register a generated model; never touch the database.

    Reject unsafe paths, malformed manifests and symlinks. No Python imports.
    """

    if not _ALIAS.fullmatch(alias):
        raise ValueError("Invalid database alias")

    root = Path(project or Path.cwd()).resolve()
    file_path = Path(destination).resolve()

    try:
        relative = file_path.relative_to(root)
    except ValueError:
        raise ValueError("Generated model must be inside the project") from None

    if file_path.suffix != ".py" or file_path.stem != name:
        raise ValueError("Generated model file does not match its class")

    if not all(part.isidentifier() for part in relative.with_suffix("").parts):
        raise ValueError("Model path cannot be imported as a Python module")

    reference = ".".join(relative.with_suffix("").parts) + ":" + name

    if not _REFERENCE.fullmatch(reference):
        raise ValueError("Invalid model reference")

    location = root / manifest
    data = _load_manifest(location)
    refs = data["databases"].setdefault(alias, [])

    if reference in refs:
        return reference, False

    refs.append(reference)
    _save_manifest(location, data)
    return reference, True


def _load_manifest(location):
    if location.is_symlink() or not location.is_file():
        raise ValueError("Model manifest must be an existing regular file")

    try:
        data = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Cannot read model manifest") from None

    if not isinstance(data, dict) or set(data) != {"databases"} or not isinstance(data["databases"], dict):
        raise ValueError("Invalid model manifest")

    for key, refs in data["databases"].items():
        if not isinstance(key, str) or not _ALIAS.fullmatch(key) or not isinstance(refs, list):
            raise ValueError("Invalid model manifest entry")

        if any(not isinstance(ref, str) or not _REFERENCE.fullmatch(ref) for ref in refs):
            raise ValueError("Invalid model reference in manifest")

        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate model reference in manifest")

    return data


def _save_manifest(location, data):
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=location.parent,
                                         prefix=".aurora-models-", suffix=".tmp", delete=False) as tmp:
            temp_path = Path(tmp.name)
            json.dump(data, tmp, indent=2)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())

        if location.is_symlink() or not location.is_file():
            raise ValueError("Refusing changed model manifest")

        os.replace(temp_path, location)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
