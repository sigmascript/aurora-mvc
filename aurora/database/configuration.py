"""Explicit, immutable AuroraSQL database aliases.

No SQLAlchemy dependency, implicit connections, or schema changes.
"""
from collections.abc import Mapping
import os
import re
from pathlib import Path
from types import MappingProxyType
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .core import connect


_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DRIVERS = frozenset(("sqlite", "sqlite+pysqlite", "mysql+pymysql", "postgresql+psycopg"))


def _canonical_url(url):
    parsed = urlsplit(url)
    if parsed.scheme == "postgres+psycopg":
        return urlunsplit(("postgresql+psycopg", parsed.netloc, parsed.path, parsed.query, parsed.fragment))
    return url


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



def _readable_url(alias, entry):
    """Convert explicit key/value settings to the existing AuroraSQL URL contract."""
    engine = entry.get("engine")
    if engine not in ("sqlite", "mysql", "postgresql", "postgres"):
        raise ValueError(f"Database '{alias}' requires engine sqlite, mysql, postgres, or postgresql")

    if engine == "postgres":
        engine = "postgresql"

    if engine == "sqlite":
        if set(entry) != {"engine", "database"}:
            raise ValueError(f"Database '{alias}' SQLite settings require only engine and database")
        database = entry["database"]
        if not isinstance(database, str) or not database or "?" in database or "#" in database:
            raise ValueError(f"Database '{alias}' has an invalid SQLite database path")
        # Keep the existing project-relative/absolute SQLite URL interpretation.
        if database == ":memory:":
            return "sqlite:///:memory:"
        path = database.replace("\\", "/")
        if path.startswith("/"):
            return "sqlite:////" + quote(path.lstrip("/"), safe="/")
        return "sqlite:///" + quote(path, safe="/")

    allowed = {"engine", "host", "port", "database", "username", "password", "password_env"}
    if set(entry) - allowed or not {"engine", "database"}.issubset(entry) or "username" not in entry:
        raise ValueError(f"Database '{alias}' requires engine, database and username")
    if "password" in entry and "password_env" in entry:
        raise ValueError(f"Database '{alias}' must choose password or password_env")

    host = entry.get("host", "localhost")
    port = entry.get("port", 3306 if engine == "mysql" else 5432)
    name = entry["database"]
    user = entry["username"]
    if not isinstance(host, str) or not host or any(ch in host for ch in "/@?#"):
        raise ValueError(f"Database '{alias}' has an invalid host")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(f"Database '{alias}' requires a TCP port from 1 to 65535")
    if (not isinstance(name, str) or not name or "/" in name or "?" in name or "#" in name
            or not isinstance(user, str) or not user):
        raise ValueError(f"Database '{alias}' has an invalid database name or username")
    password = entry.get("password")
    if "password_env" in entry:
        variable = entry["password_env"]
        if not isinstance(variable, str) or not _ENV.fullmatch(variable):
            raise ValueError(f"Database '{alias}' has an invalid password environment variable name")
        password = os.environ.get(variable)
        if password is None or not password:
            raise ValueError(f"Database '{alias}' requires nonempty environment variable '{variable}'")
    if password is not None and not isinstance(password, str):
        raise ValueError(f"Database '{alias}' has an invalid password")
    address = f"[{host}]" if ":" in host and not host.startswith("[") else host
    credentials = quote(user, safe="")
    if password is not None:
        credentials += ":" + quote(password, safe="")
    driver = "mysql+pymysql" if engine == "mysql" else "postgresql+psycopg"
    return f"{driver}://{credentials}@{address}:{port}/{quote(name, safe='')}"


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

            if not isinstance(entry, Mapping):
                raise ValueError(f"Database '{alias}' must be a mapping")

            settings = entry
            # URL and environment-backed URL remain supported optional forms.
            if set(settings) in ({"url"}, {"url_env"}):
                url = None
            elif "url" in settings or "url_env" in settings:
                raise ValueError(f"Database '{alias}' cannot mix URL and readable connection settings")
            else:
                url = _readable_url(alias, settings)

            if "url_env" in settings:
                name = settings["url_env"]

                if not isinstance(name, str) or not _ENV.fullmatch(name):
                    raise ValueError(f"Database '{alias}' has an invalid environment variable name")

                url = os.environ.get(name)

                if not url or not url.strip():
                    raise ValueError(f"Database '{alias}' requires a nonempty environment variable '{name}'")
            elif "url" in settings:
                url = settings["url"]

            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"Database '{alias}' requires a nonempty string URL")

            # Normalize the short PostgreSQL driver synonym before routing.
            url = _canonical_url(url)
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
