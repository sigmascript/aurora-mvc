"""Explicit project model registration without module scanning or database I/O.

A project imports the models it intends to use and passes their classes here.
This is deliberately independent of configuration parsing and database binding.
"""
from collections.abc import Iterable, Mapping

from aurora.database.registry import ModelRegistry


def register_project_models(
    *,
    shared: Iterable[type] = (),
    apps: Mapping[str, Iterable[type]] | None = None,
    registry: ModelRegistry | None = None,
) -> ModelRegistry:
    """Build an explicit registry from already-imported model classes.

    Validate the entire candidate set before mutating an existing registry.
    Model classes must already be defined using Aurora's Model API.
    """
    target = registry if registry is not None else ModelRegistry()
    candidate = ModelRegistry()
    # Seed a candidate registry to avoid partial mutation on a conflict.
    for entry in target.list():
        candidate.register(entry.model, app=entry.app)
    for model in shared:
        candidate.register(model)
    for app, models in (apps or {}).items():
        for model in models:
            candidate.register(model, app=app)
    # Commit only after all validation succeeds.
    for entry in candidate.list():
        target.register(entry.model, app=entry.app)
    return target
