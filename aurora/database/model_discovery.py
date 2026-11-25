"""Model discovery, manifest selection, identity, and CLI registration.

Keep project-file validation and registration together without importing
application models during discovery. Model identity evidence only validates
model classes explicitly supplied by a caller.
"""

from __future__ import annotations

import ast
import json
import keyword
import os
import re
import tempfile
from pathlib import Path

from .models import Model, _identifier

# Discovery and persistent-identity validation.
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CLASS = re.compile(r"[A-Z][A-Za-z0-9]*\Z")
_MODEL_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*\Z")

# Explicit manifest records and CLI registration have different alias rules.
_REF = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
_MANIFEST_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_REFERENCE = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$")
_REGISTRATION_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")


# Model manifest selection and validation.


def select_model_refs(*, explicit=(), database="default", manifest="aurora_models.json"):
    """Return explicit refs if supplied, otherwise refs for the selected alias.

    Manifest format: {"databases": {"default": ["pkg.models:Model"]}}.
    No fallback across aliases; no automatic discovery.
    """

    if explicit:
        refs = list(explicit)
    else:
        refs = read_model_mapping(manifest).get(database, [])

    if any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in refs):
        raise ValueError("Invalid MODULE:CLASS model reference")

    if len(set(refs)) != len(refs):
        raise ValueError("Duplicate model references")

    return tuple(refs)


def read_model_mapping(manifest):
    """Read optional model assignments without requiring a populated manifest."""
    path = Path(manifest).expanduser()

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read model manifest: {path}: {exc}") from exc

    if (
        not isinstance(raw, dict)
        or set(raw) != {"databases"}
        or not isinstance(raw["databases"], dict)
    ):
        raise ValueError("Model manifest must contain only a databases mapping")

    mapping = raw["databases"]

    for alias, items in mapping.items():
        if (
            not isinstance(alias, str)
            or not _MANIFEST_ALIAS.fullmatch(alias)
            or not isinstance(items, list)
        ):
            raise ValueError("Invalid model manifest database entry")

        if any(not isinstance(item, str) or not _REF.fullmatch(item) for item in items):
            raise ValueError(f"Invalid model references for database {alias}")

        if len(set(items)) != len(items):
            raise ValueError(f"Duplicate model references for database {alias}")

    assigned = [ref for refs in mapping.values() for ref in refs]

    if len(set(assigned)) != len(assigned):
        raise ValueError("One model cannot be assigned to multiple databases")

    return mapping


# Conventional project model discovery.


def validate_model_source(project_root, module_name, class_name):
    """Validate conventional model identity before importing project Python."""
    root = Path(project_root).resolve()
    path = root.joinpath(*module_name.split(".")).with_suffix(".py")

    if (
        path.is_symlink()
        or not path.is_file()
        or not path.resolve().is_relative_to(root)
        or not _CLASS.fullmatch(class_name)
        or path.stem != class_name
    ):
        raise ValueError(f"Model {module_name}:{class_name} requires a matching CamelCase file")

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeError, OSError) as error:
        raise ValueError(f"Cannot parse model file {path}: {error}") from error

    models = [
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            (isinstance(base, ast.Name) and base.id == "Model")
            or (isinstance(base, ast.Attribute) and base.attr == "Model")
            for base in node.bases
        )
    ]

    if models != [class_name]:
        raise ValueError(f"Model file {path} must define exactly one Model subclass: {class_name}")


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
            if (
                file.is_symlink()
                or not file.is_file()
                or not _CLASS.fullmatch(file.stem)
                or keyword.iskeyword(file.stem)
            ):
                continue

            ref = f"{module}.{file.stem}:{file.stem}"

            if assignments.get(ref, "default") == database:
                found.add(ref)

    refs = tuple(sorted(found | set(mapping.get(database, ()))))
    by_name = {}

    for ref in refs:
        name = ref.rsplit(":", 1)[1]
        previous = by_name.get(name)
        if previous is not None:
            raise ValueError(
                f"Duplicate model name {name!r} in database {database!r}: {previous} and {ref}"
            )
        by_name[name] = ref

    return refs


# Stable model-to-table identity evidence.


def model_identity_map(models):
    """Describe selected models without importing arbitrary historical names."""
    mapping = {}
    tables = set()

    for model in models:
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise TypeError("Expected AuroraSQL model classes")

        reference = f"{model.__module__}:{model.__qualname__}"
        _identifier(model.__table_name__)

        if not _MODEL_REF.fullmatch(reference):
            # Local/dynamic classes cannot provide persistent rename identity.
            # They may still be used for ordinary model-driven operations.
            reference = f"untrusted:{model.__table_name__}"

        if reference in mapping or model.__table_name__ in tables:
            raise ValueError("Duplicate model identity or physical table")

        mapping[reference] = model.__table_name__
        tables.add(model.__table_name__)

    return dict(sorted(mapping.items()))


def canonical_model_map(mapping):
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("Migration model identity mapping is missing")

    if any(
        not isinstance(key, str) or not _MODEL_REF.fullmatch(key) or not isinstance(value, str)
        for key, value in mapping.items()
    ):
        raise ValueError("Invalid model identity mapping")

    for table in mapping.values():
        _identifier(table)

    if len(set(mapping.values())) != len(mapping):
        raise ValueError("Ambiguous model-to-table identity mapping")

    return json.dumps(mapping, sort_keys=True, separators=(",", ":"))


def table_rename_candidates(previous, models):
    """Infer only unambiguous stable model identity changes; never execute DDL."""
    current = model_identity_map(models)
    previous_tables = set(previous.values())
    current_tables = set(current.values())
    candidates = []

    for reference, new_table in current.items():
        old_table = previous.get(reference)

        if old_table is None or old_table == new_table:
            continue

        if (
            reference.startswith("untrusted:")
            or old_table in current_tables
            or new_table in previous_tables
        ):
            raise ValueError(
                "Table rename identity is ambiguous; explicit reviewed intent required"
            )

        candidates.append((reference, old_table, new_table))

    return tuple(sorted(candidates))


# Atomic generated-model registration.


def register_generated_model(
    destination, name, *, project=None, alias=None, manifest="aurora_models.json"
):
    """Atomically register a generated model; never touch the database.

    Reject unsafe paths, malformed manifests and symlinks. No Python imports.
    """

    root = Path(project or Path.cwd()).resolve()

    if alias is None:
        from .cli_check import _load_config, resolve_project_database

        config, _ = _load_config(root / "aurora_databases.json")
        alias = resolve_project_database(config, root)

    if not isinstance(alias, str) or not _REGISTRATION_ALIAS.fullmatch(alias):
        raise ValueError("Invalid database name")
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

    if (
        not isinstance(data, dict)
        or set(data) != {"databases"}
        or not isinstance(data["databases"], dict)
    ):
        raise ValueError("Invalid model manifest")

    for key, refs in data["databases"].items():
        if (
            not isinstance(key, str)
            or not _REGISTRATION_ALIAS.fullmatch(key)
            or not isinstance(refs, list)
        ):
            raise ValueError("Invalid model manifest entry")

        if any(not isinstance(ref, str) or not _REFERENCE.fullmatch(ref) for ref in refs):
            raise ValueError("Invalid model reference in manifest")

        if len(set(refs)) != len(refs):
            raise ValueError("Duplicate model reference in manifest")

    return data


def _save_manifest(location, data):
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=location.parent,
            prefix=".aurora-models-",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            json.dump(data, tmp, indent=4)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())

        if location.is_symlink() or not location.is_file():
            raise ValueError("Refusing changed model manifest")

        os.replace(temp_path, location)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
