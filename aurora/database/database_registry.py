"""Lazy named Database instances without changing Model.bind or global state.

The registry owns only databases it creates. Call close() at application shutdown.
No cross-database transaction semantics are implied.
"""
from threading import RLock

from aurora.database.database import Database
from aurora.database.database_config import DatabaseConfiguration


class DatabaseRegistry:
    """Resolve configured aliases to independently owned, lazy Database objects."""

    def __init__(self, configuration: DatabaseConfiguration):
        if not isinstance(configuration, DatabaseConfiguration):
            raise TypeError("configuration must be a DatabaseConfiguration")
        self._configuration = configuration
        self._instances: dict[str, Database] = {}
        self._lock = RLock()
        self._closed = False

    def aliases(self) -> tuple[str, ...]:
        return self._configuration.aliases()

    def get(self, alias: str = "default") -> Database:
        # Validate before attempting engine construction, including after close.
        url = self._configuration.resolve(alias)
        with self._lock:
            if self._closed:
                raise RuntimeError("Database registry is closed")
            if alias not in self._instances:
                self._instances[alias] = Database(url)
            return self._instances[alias]

    def initialized_aliases(self) -> tuple[str, ...]:
        """Aliases with instantiated engines; does not open connections."""
        with self._lock:
            return tuple(self._instances)

    def close(self) -> None:
        """Dispose all owned engines. Idempotent; prevents future get()."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            instances = tuple(self._instances.values())
            self._instances.clear()
        for db in instances:
            db.dispose()

    def __enter__(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("Database registry is closed")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


def create_database_registry(configuration: DatabaseConfiguration) -> DatabaseRegistry:
    """Create an empty lazy registry; does not instantiate engines."""
    return DatabaseRegistry(configuration)
