"""Explicit model catalog, independent of database connections and migrations.

Registration does not import modules, create tables, or mutate Model.metadata.
"""
from dataclasses import dataclass

from aurora.database.model import Model


@dataclass(frozen=True)
class ModelEntry:
    model: type[Model]
    name: str
    table: str
    app: str | None = None

    @property
    def qualified_name(self) -> str:
        return f"{self.app}.{self.name}" if self.app else self.name


class ModelRegistry:
    """Explicit catalog of existing Model classes for a single database scope.

    A future named-database registry can own one catalog per database alias.
    """

    def __init__(self):
        self._by_table: dict[str, ModelEntry] = {}
        self._by_name: dict[str, ModelEntry] = {}

    def register(self, model: type[Model], *, app: str | None = None) -> ModelEntry:
        if not isinstance(model, type) or not issubclass(model, Model) or model is Model:
            raise TypeError("Expected an Aurora Model subclass")
        if app is not None and (not isinstance(app, str) or not app.isidentifier()):
            raise ValueError("App must be a valid Python identifier")
        table = model.__sql_table__
        if table.metadata is not Model.metadata:
            raise ValueError("Model does not use Aurora shared metadata")
        if Model.metadata.tables.get(table.key) is not table:
            raise ValueError("Model table is not the registered metadata table")
        entry = ModelEntry(model, model.__name__, table.key, app)
        existing = self._by_table.get(entry.table)
        if existing is not None:
            if existing == entry:
                return existing
            raise ValueError(f"Table already registered: {entry.table} ({existing.qualified_name})")
        existing = self._by_name.get(entry.qualified_name)
        if existing is not None:
            if existing == entry:
                return existing
            raise ValueError(f"Model name already registered: {entry.qualified_name}")
        self._by_table[entry.table] = entry
        self._by_name[entry.qualified_name] = entry
        return entry

    def get(self, name: str) -> ModelEntry:
        """Resolve a qualified name, or an unambiguous short class name."""
        if name in self._by_name:
            return self._by_name[name]
        matches = [entry for entry in self._by_name.values() if entry.name == name]
        if not matches:
            raise KeyError(name)
        if len(matches) > 1:
            raise ValueError(f"Ambiguous model name: {name}; use app.ModelName")
        return matches[0]

    def list(self, *, app: str | None = None) -> tuple[ModelEntry, ...]:
        return tuple(sorted((e for e in self._by_name.values() if app is None or e.app == app), key=lambda e: e.qualified_name))

    def __len__(self) -> int:
        return len(self._by_name)
