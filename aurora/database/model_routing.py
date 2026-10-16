"""Explicit, immutable model-to-database routing *contract*.

No engines, sessions, model binding, or metadata are modified here.
"""
from collections.abc import Mapping
from types import MappingProxyType

from aurora.database.database_config import DatabaseConfiguration
from aurora.database.registry import ModelRegistry


class ModelDatabaseRouting:
    """Resolve registered models to named database aliases without I/O.

    Routes may be keyed by a registered model class or its qualified registry
    name. Unspecified models use 'default'. Every configured alias is checked.
    """

    def __init__(self, models: ModelRegistry, databases: DatabaseConfiguration,
                 routes: Mapping | None = None):
        if not isinstance(models, ModelRegistry):
            raise TypeError("models must be a ModelRegistry")
        if not isinstance(databases, DatabaseConfiguration):
            raise TypeError("databases must be a DatabaseConfiguration")
        if routes is not None and not isinstance(routes, Mapping):
            raise TypeError("routes must be a mapping")
        entries = models.list()
        by_class = {entry.model: entry for entry in entries}
        by_name = {entry.qualified_name: entry for entry in entries}
        resolved = {entry.model: "default" for entry in entries}
        assigned = set()
        for key, alias in (routes or {}).items():
            if isinstance(key, str):
                # Qualified names are required: short-name fallback would be
                # ambiguous when apps contain identically named models.
                entry = by_name.get(key)
            else:
                try:
                    entry = by_class.get(key)
                except TypeError:
                    entry = None
            if entry is None:
                raise ValueError("Route references an unregistered model")
            if entry.model in assigned:
                raise ValueError(f"Duplicate route for {entry.qualified_name}")
            databases.resolve(alias)
            assigned.add(entry.model)
            resolved[entry.model] = alias
        self._routes = MappingProxyType(resolved)
        self._names = MappingProxyType(by_name)

    def resolve(self, model: type | str) -> str:
        if isinstance(model, str):
            entry = self._names.get(model)
            if entry is None:
                raise KeyError(model)
            model = entry.model
        try:
            return self._routes[model]
        except (KeyError, TypeError):
            raise KeyError("Model is not registered in routing contract") from None

    def assignments(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((name, self._routes[entry.model])
                            for name, entry in self._names.items()))


def configure_model_routing(models: ModelRegistry, databases: DatabaseConfiguration,
                            routes: Mapping | None = None) -> ModelDatabaseRouting:
    return ModelDatabaseRouting(models, databases, routes)
