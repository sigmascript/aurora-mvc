"""Read-only, explicitly scoped model-to-schema status for the database CLI."""
from __future__ import annotations

import importlib
from pathlib import Path

from aurora.database.cli_inspection import plan_database
from aurora.database.model import Model


def compare_selected_models(config, alias: str, references: list[str], *, base_dir: Path):
    """Import trusted, explicitly named models and compare without database writes."""
    if not references:
        raise ValueError("Schema status requires explicit --model MODULE:CLASS")
    models = []
    for reference in references:
        module_name, separator, class_name = reference.partition(":")
        if (not separator or not module_name or not class_name.isidentifier()
                or not all(part.isidentifier() for part in module_name.split("."))):
            raise ValueError("--model must be MODULE:CLASS")
        cls = getattr(importlib.import_module(module_name), class_name, None)
        if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
            raise ValueError(f"Not an Aurora model: {reference}")
        models.append(cls)
    return plan_database(config, alias, models, base_dir=base_dir)
