"""Explicit project database runtime; no implicit configuration discovery."""
from aurora.database.database_config import DatabaseConfiguration
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import ModelDatabaseRouting
from aurora.database.routed_access import RoutedDatabaseAccess
from aurora.database.async_routed import RoutedWorkManager


def configure_project_databases(databases, routing, *, max_concurrent=8):
    """Build lazy named engines and a bounded work manager from explicit inputs.

    ``routing`` must be an existing ModelDatabaseRouting contract. The caller
    owns the returned registry and manager; pass both to Application for ASGI
    shutdown coordination. No engines are initialized by this function.
    """
    config = DatabaseConfiguration(databases)
    if not isinstance(routing, ModelDatabaseRouting):
        raise TypeError("routing must be a ModelDatabaseRouting")
    registry = DatabaseRegistry(config)
    access = RoutedDatabaseAccess(routing, registry)
    manager = RoutedWorkManager(access, max_concurrent=max_concurrent)
    return registry, access, manager
