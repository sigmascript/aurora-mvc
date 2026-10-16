"""Explicit model database access without mutating Model.bind().

This adapter resolves a registered model to a lazy database instance. It does
not redirect existing Model CRUD or imply cross-database transactions.
"""

from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import ModelDatabaseRouting


class RoutedDatabaseAccess:
    """Resolve registered model classes or qualified names to Database objects."""

    def __init__(self, routing: ModelDatabaseRouting, databases: DatabaseRegistry):
        if not isinstance(routing, ModelDatabaseRouting):
            raise TypeError("routing must be a ModelDatabaseRouting")
        if not isinstance(databases, DatabaseRegistry):
            raise TypeError("databases must be a DatabaseRegistry")
        self._routing = routing
        self._databases = databases
        # Fail before initializing any engine when configurations mismatch.
        available = set(databases.aliases())
        for _, alias in routing.assignments():
            if alias not in available:
                raise ValueError(f"Routing alias is unavailable: {alias}")

    def alias_for(self, model: type | str) -> str:
        """Resolve alias without creating an engine or opening a connection."""
        return self._routing.resolve(model)

    def database_for(self, model: type | str):
        """Get the lazily constructed Database for a registered model."""
        return self._databases.get(self.alias_for(model))
