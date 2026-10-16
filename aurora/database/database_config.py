"""Optional named database configuration; no engine or connection creation.

The existing Database(url) and Model.bind() interfaces remain unchanged.
"""
from collections.abc import Mapping
from types import MappingProxyType
import re

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")


class DatabaseConfiguration:
    """Validated immutable named URL registry; always includes 'default'.

    Accepts {alias: {"url": url}}. SQLAlchemy URL objects are supported.
    No connection is opened and credentials are not printed in errors/repr.
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
            if not isinstance(entry, Mapping) or set(entry) != {"url"}:
                raise ValueError(f"Database '{alias}' requires exactly a 'url' setting")
            url = entry["url"]
            if not isinstance(url, str) and not hasattr(url, "get_backend_name"):
                raise TypeError(f"Database '{alias}' URL must be a string or SQLAlchemy URL")
            if isinstance(url, str) and not url.strip():
                raise ValueError(f"Database '{alias}' URL must not be empty")
            try:
                parsed = make_url(url)
            except (ValueError, TypeError, AttributeError, ArgumentError):
                raise ValueError(f"Database '{alias}' has an invalid URL") from None
            if not parsed.drivername:
                raise ValueError(f"Database '{alias}' has an invalid URL")
            resolved[alias] = parsed
        self._urls = MappingProxyType(resolved)

    def aliases(self) -> tuple[str, ...]:
        return tuple(self._urls)

    def resolve(self, alias: str = "default"):
        """Return SQLAlchemy URL for a known alias; no engine is created."""
        if alias not in self._urls:
            raise ValueError(f"Unknown database alias: {alias!r}")
        return self._urls[alias]

    def __repr__(self):
        return f"DatabaseConfiguration(aliases={self.aliases()!r})"


def configure_databases(databases: Mapping) -> DatabaseConfiguration:
    """Validate DATABASES without touching the database or global Model state."""
    return DatabaseConfiguration(databases)
