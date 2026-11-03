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


def canonical_model_reference(reference, *, project=None):
    """Use project-root-relative references, without importing untrusted code.

    Accept an optional project-name prefix only when its corresponding local
    module file exists. Other references remain untouched.
    """

    if not isinstance(reference, str) or not _REFERENCE.fullmatch(reference):
        raise ValueError("Expected a MODULE:CLASS model reference")

    root = Path(project or Path.cwd()).resolve()
    module, class_name = reference.split(":", 1)
    prefix = root.name + "."

    if module.startswith(prefix):
        local = module[len(prefix):]
        relative = Path(*local.split("."))
        candidate = root / relative.with_suffix(".py")
        package = root / relative / "__init__.py"

        if candidate.is_file() or package.is_file():
            return local + ":" + class_name

    return reference


def register_model(reference, *, database="default", manifest="aurora_models.json", project=None):
    """Atomically register a canonical reference; no imports or database writes."""

    if not isinstance(database, str) or not _ALIAS.fullmatch(database):
        raise ValueError("Invalid database alias")

    root = Path(project or Path.cwd()).resolve()
    canonical = canonical_model_reference(reference, project=root)
    location = Path(manifest).expanduser()

    if not location.is_absolute():
        location = root / location

    data = _load_manifest(location)
    refs = data["databases"].setdefault(database, [])
    # Do not silently rewrite existing manifests or collapse unrelated refs.
    existing = [canonical_model_reference(ref, project=root) for ref in refs]

    if len(set(existing)) != len(existing):
        raise ValueError("Duplicate equivalent model references in manifest")

    if canonical in existing:
        return False

    refs.append(canonical)
    _save_manifest(location, data)
    return True


def registered_models(*, database="default", manifest="aurora_models.json", project=None):
    """List model references registered for a database alias."""

    if not isinstance(database, str) or not _ALIAS.fullmatch(database):
        raise ValueError("Invalid database alias")

    root = Path(project or Path.cwd()).resolve()
    location = Path(manifest).expanduser()

    if not location.is_absolute():
        location = root / location

    data = _load_manifest(location)
    refs = data["databases"].get(database, [])
    canonical = tuple(canonical_model_reference(ref, project=root) for ref in refs)

    if len(set(canonical)) != len(canonical):
        raise ValueError("Duplicate equivalent model references in manifest")

    return canonical


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
