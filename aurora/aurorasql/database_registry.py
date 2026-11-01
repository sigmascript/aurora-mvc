"""Lazy AuroraSQL connection registry for optional ASGI application lifecycle.

Each alias owns a connection that is closed during application shutdown.
Do not share these connections across worker threads; use the async work
manager for request-level work instead.
"""

from threading import RLock

from .configuration import DatabaseConfiguration


class DatabaseRegistry:
    def __init__(self, configuration: DatabaseConfiguration):
        if not isinstance(configuration, DatabaseConfiguration):
            raise TypeError("configuration must be a DatabaseConfiguration")
        self._configuration = configuration
        self._instances = {}
        self._lock = RLock()
        self._closed = False

    def aliases(self):
        return self._configuration.aliases()

    def get(self, alias="default"):
        with self._lock:
            if self._closed:
                raise RuntimeError("Database registry is closed")
            self._configuration.resolve(alias)
            if alias not in self._instances:
                self._instances[alias] = self._configuration.connect(alias)
            return self._instances[alias]

    def initialized_aliases(self):
        with self._lock:
            return tuple(self._instances)

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            instances = tuple(self._instances.values())
            self._instances.clear()
        failure = None
        for connection in instances:
            try:
                connection.close()
            except Exception as error:
                # One failed close must not strand connections for other aliases.
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    def __enter__(self):
        if self._closed:
            raise RuntimeError("Database registry is closed")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
