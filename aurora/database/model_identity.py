"""Conservative model-to-table identity evidence for reviewed schema history."""

import json
import re

from .models import Model, _identifier


_MODEL_REF = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*\Z")


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

    if any(not isinstance(key, str) or not _MODEL_REF.fullmatch(key)
           or not isinstance(value, str) for key, value in mapping.items()):
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

        if (reference.startswith("untrusted:") or old_table in current_tables
                or new_table in previous_tables):
            raise ValueError("Table rename identity is ambiguous; explicit reviewed intent required")

        candidates.append((reference, old_table, new_table))

    return tuple(sorted(candidates))
