"""Validated, immutable named database URLs without opening connections."""

from collections.abc import Mapping
import os
import re
from types import MappingProxyType

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_SUPPORTED_DRIVERS = frozenset({"sqlite", "sqlite+pysqlite", "mysql+pymysql"})


class DatabaseConfiguration:
    """Named SQLAlchemy URLs with an explicit default database.

    Supports SQLite and MySQL via PyMySQL. Environment values are resolved
    once at construction; configuration never opens database connections.
    credentials are never included in Aurora validation errors or reprs.
    """

    def __init__(self, databases: Mapping):
        if not isinstance(databases, Mapping):
            raise TypeError("DATABASES must be a mapping")
        if "default" not in databases:
            raise ValueError("DATABASES must define a 'default' database")

        resolved = {}
        for alias, entry in databases.items():
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ValueError("Database aliases must be lowercase identifiers")
            if not isinstance(entry, Mapping) or len(entry) != 1 or not (set(entry) == {"url"} or set(entry) == {"url_env"}):
                raise ValueError(f"Database '{alias}' requires exactly one of 'url' or 'url_env'")

            if "url_env" in entry:
                name = entry["url_env"]
                if not isinstance(name, str) or not _ENV_NAME.fullmatch(name):
                    raise ValueError(f"Database '{alias}' has an invalid environment variable name")
                url = os.environ.get(name)
                if url is None or not url.strip():
                    raise ValueError(f"Database '{alias}' requires a nonempty environment variable '{name}'")
            else:
                url = entry["url"]
            if not isinstance(url, (str, URL)):
                raise TypeError(f"Database '{alias}' URL must be a string or SQLAlchemy URL")
            if isinstance(url, str) and not url.strip():
                raise ValueError(f"Database '{alias}' URL must not be empty")

            try:
                parsed = make_url(url)
            except (ValueError, TypeError, AttributeError, ArgumentError):
                raise ValueError(f"Database '{alias}' has an invalid URL") from None

            if parsed.drivername not in _SUPPORTED_DRIVERS:
                raise ValueError(
                    f"Database '{alias}' uses an unsupported driver; "
                    "supported drivers: sqlite, sqlite+pysqlite, mysql+pymysql"
                )
            if parsed.drivername == "mysql+pymysql" and not parsed.database:
                raise ValueError(f"Database '{alias}' requires a MySQL database name")
            resolved[alias] = parsed

        self._urls = MappingProxyType(resolved)

    def aliases(self) -> tuple[str, ...]:
        return tuple(self._urls)

    def resolve(self, alias: str = "default") -> URL:
        """Return a configured URL without creating an engine."""
        if alias not in self._urls:
            raise ValueError(f"Unknown database alias: {alias!r}")
        return self._urls[alias]

    def __repr__(self):
        return f"DatabaseConfiguration(aliases={self.aliases()!r})"


def configure_databases(databases: Mapping) -> DatabaseConfiguration:
    """Validate named databases without changing Model state or opening connections."""
    return DatabaseConfiguration(databases)
