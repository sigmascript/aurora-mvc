"""Explicit, immutable AuroraSQL database aliases.

No SQLAlchemy dependency, implicit connections, or schema changes.
"""
from collections.abc import Mapping
import os
import re
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from .core import connect


_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DRIVERS = frozenset(("sqlite", "sqlite+pysqlite", "mysql+pymysql", "postgresql+psycopg"))


def _destination_identity(parsed):
    """Prove selected URL destinations equal; never contact a database server.

    A matching identity establishes a collision. Different identities do not
    prove that different hostnames or filesystem mounts reach distinct storage.
    """
    if parsed.scheme.startswith("sqlite"):
        # Match core.connect's SQLite path interpretation, including percent
        # decoding and the difference between relative and absolute URLs.
        path = unquote(parsed.path)
        if path == "/:memory:":
            return None  # Independent connection-local databases.
        if path.startswith("///"):
            database = path[2:]
        elif path.startswith("//"):
            database = path[1:]
        else:
            database = path[1:]
        if not database:
            return None  # SQLite's temporary unnamed database.
        return ("sqlite", os.path.normcase(str(Path(database).resolve(strict=False))))

    # Same driver family, server endpoint and database name is a collision
    # regardless of which credentials are used. Do not include secrets.
    backend = parsed.scheme.split("+", 1)[0]
    port = parsed.port or {"mysql": 3306, "postgresql": 5432}[backend]
    return (backend, parsed.hostname.rstrip("."), port, unquote(parsed.path.lstrip("/")))


class DatabaseConfiguration:
    """Resolve named AuroraSQL URLs once, without opening connections."""

    def __init__(self, databases):
        # Reject malformed configuration before resolving any connection URLs.
        if not isinstance(databases, Mapping):
            raise TypeError("DATABASES must be a mapping")

        if "default" not in databases:
            raise ValueError("DATABASES must define a 'default' database")

        # Resolve aliases eagerly so later lookups cannot depend on changed environment values.
        resolved = {}
        destinations = {}

        for alias, entry in databases.items():
            if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
                raise ValueError("Database aliases must be lowercase identifiers")

            if (
                not isinstance(entry, Mapping)
                or len(entry) != 1
                or set(entry) not in ({"url"}, {"url_env"})
            ):
                raise ValueError(f"Database '{alias}' requires exactly one of 'url' or 'url_env'")

            # Each alias chooses either a literal URL or an environment variable.
            if "url_env" in entry:
                name = entry["url_env"]

                if not isinstance(name, str) or not _ENV.fullmatch(name):
                    raise ValueError(f"Database '{alias}' has an invalid environment variable name")

                url = os.environ.get(name)

                if not url or not url.strip():
                    raise ValueError(f"Database '{alias}' requires a nonempty environment variable '{name}'")
            else:
                url = entry["url"]

            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"Database '{alias}' requires a nonempty string URL")

            # Validate URL structure without opening a database connection.
            try:
                parsed = urlsplit(url)

                if parsed.scheme not in _DRIVERS or parsed.query or parsed.fragment:
                    raise ValueError

                if parsed.scheme.startswith("sqlite"):
                    if parsed.netloc or not parsed.path.startswith("/"):
                        raise ValueError
                elif not parsed.hostname or not parsed.path.strip("/"):
                    raise ValueError

                if parsed.port is not None and parsed.port <= 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Database '{alias}' has an invalid or unsupported URL") from None

            identity = _destination_identity(parsed)
            if identity is not None:
                previous = destinations.get(identity)
                if previous is not None:
                    raise ValueError(
                        f"Database '{alias}' resolves to the same destination as "
                        f"database '{previous}'; configure one canonical name"
                    )
                destinations[identity] = alias

            resolved[alias] = url

        self._urls = MappingProxyType(resolved)

    def aliases(self):
        return tuple(self._urls)

    def resolve(self, alias="default"):
        if alias not in self._urls:
            raise ValueError(f"Unknown database alias: {alias!r}")

        return self._urls[alias]

    def connect(self, alias="default"):
        """Open an explicitly selected connection; caller owns its lifetime."""
        return connect(self.resolve(alias))

    def __repr__(self):
        return f"DatabaseConfiguration(aliases={self.aliases()!r})"


def configure_databases(databases):
    """Install the explicit database alias configuration without connecting."""
    return DatabaseConfiguration(databases)
