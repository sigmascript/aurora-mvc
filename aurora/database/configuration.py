"""Explicit, immutable AuroraSQL named database connections.

No SQLAlchemy dependency, implicit connections, or schema changes.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .connections import connect

_ALIAS = re.compile(r"^[a-z][a-z0-9_]*$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DRIVERS = frozenset(("sqlite", "sqlite+pysqlite", "mysql+pymysql", "postgresql+psycopg"))


def _canonical_url(url):
    parsed = urlsplit(url)
    if parsed.scheme == "postgres+psycopg":
        return urlunsplit(
            ("postgresql+psycopg", parsed.netloc, parsed.path, parsed.query, parsed.fragment)
        )
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


def _readable_url(name, entry):
    """Convert explicit key/value settings to the existing AuroraSQL URL contract."""
    engine = entry.get("engine")
    if engine not in ("sqlite", "mysql", "postgresql", "postgres"):
        raise ValueError(
            f"Database '{name}' requires engine sqlite, mysql, postgres, or postgresql"
        )

    if engine == "postgres":
        engine = "postgresql"

    if engine == "sqlite":
        if "database" in entry:
            raise ValueError(
                f"Database '{name}' SQLite accepts only engine and optional extension; remove database"
            )
        if set(entry) - {"engine", "extension"}:
            raise ValueError(f"Database '{name}' SQLite accepts only engine and optional extension")
        extension = entry.get("extension", "sqlite3")
        if type(extension) is not str or extension not in ("sqlite3", "sqlite", "db", ""):
            raise ValueError(f"Database '{name}' has an invalid SQLite extension")
        filename = name + ("." + extension if extension else "")
        return "sqlite:///" + quote(filename, safe="")

    allowed = {"engine", "host", "port", "username", "password", "password_env"}
    if "database" in entry:
        raise ValueError(f"Database '{name}' requires engine and username; remove database")
    if set(entry) - allowed:
        raise ValueError(f"Database '{name}' has unknown connection settings")
    if "username" not in entry:
        raise ValueError(f"Database '{name}' requires username")
    if "password" in entry and "password_env" in entry:
        raise ValueError(f"Database '{name}' must choose password or password_env")

    host = entry.get("host", "localhost")
    port = entry.get("port", 3306 if engine == "mysql" else 5432)
    name = name
    user = entry["username"]
    if not isinstance(host, str) or not host or any(ch in host for ch in "/@?#"):
        raise ValueError(f"Database '{name}' has an invalid host")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(f"Database '{name}' requires a TCP port from 1 to 65535")
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or "?" in name
        or "#" in name
        or not isinstance(user, str)
        or not user
    ):
        raise ValueError(f"Database '{name}' has an invalid database name or username")
    password = entry.get("password")
    if "password_env" in entry:
        variable = entry["password_env"]
        if not isinstance(variable, str) or not _ENV.fullmatch(variable):
            raise ValueError(f"Database '{name}' has an invalid password environment variable name")
        password = os.environ.get(variable)
        if password is None or not password:
            raise ValueError(
                f"Database '{name}' requires nonempty environment variable '{variable}'"
            )
    if password is not None and not isinstance(password, str):
        raise ValueError(f"Database '{name}' has an invalid password")
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
        if not databases:
            raise ValueError("DATABASES must contain at least one named database")

        # Resolve names eagerly so later lookups cannot depend on changed environment values.
        resolved = {}
        destinations = {}

        for name, entry in databases.items():
            if not isinstance(name, str) or not _ALIAS.fullmatch(name):
                raise ValueError("Database names must be lowercase identifiers")

            if not isinstance(entry, Mapping):
                raise ValueError(f"Database '{name}' must be a mapping")

            settings = entry
            # URL and environment-backed URL remain supported optional forms.
            if set(settings) in ({"url"}, {"url_env"}):
                url = None
            elif "url" in settings or "url_env" in settings:
                raise ValueError(
                    f"Database '{name}' cannot mix URL and readable connection settings"
                )
            else:
                url = _readable_url(name, settings)

            if "url_env" in settings:
                variable = settings["url_env"]

                if not isinstance(variable, str) or not _ENV.fullmatch(variable):
                    raise ValueError(f"Database '{name}' has an invalid environment variable name")

                url = os.environ.get(variable)

                if not url or not url.strip():
                    raise ValueError(
                        f"Database '{name}' requires a nonempty environment variable '{variable}'"
                    )
            elif "url" in settings:
                url = settings["url"]

            if not isinstance(url, str) or not url.strip():
                raise ValueError(f"Database '{name}' requires a nonempty string URL")

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
                raise ValueError(f"Database '{name}' has an invalid or unsupported URL") from None

            # The configured database name must match its physical destination.
            # URL-based definitions must obey the same rule as readable settings.
            physical = unquote(parsed.path.lstrip("/"))
            if parsed.scheme.startswith("sqlite"):
                raw_path = unquote(parsed.path)
                if raw_path in ("/:memory:", "") or raw_path.startswith("//"):
                    raise ValueError(
                        f"Database '{name}' URL must identify a project-relative SQLite file"
                    )
                filename = Path(physical).name
                extension = Path(filename).suffix.lstrip(".")
                stem = filename[: -(len(extension) + 1)] if extension else filename
                if (
                    "/" in physical
                    or "\\" in physical
                    or stem != name
                    or extension not in ("sqlite3", "sqlite", "db", "")
                ):
                    raise ValueError(f"Database '{name}' URL does not match its configured name")
            elif physical != name:
                raise ValueError(f"Database '{name}' URL does not match its configured name")

            identity = _destination_identity(parsed)
            if identity is not None:
                previous = destinations.get(identity)
                if previous is not None:
                    raise ValueError(
                        f"Database '{name}' resolves to the same destination as "
                        f"database '{previous}'; use one configured name"
                    )
                destinations[identity] = name

            resolved[name] = url

        self._urls = MappingProxyType(resolved)

    def names(self):
        return tuple(self._urls)

    def resolve(self, name="default"):
        if name not in self._urls:
            raise ValueError(f"Unknown database name: {name!r}")

        return self._urls[name]

    def connect(self, name="default"):
        """Open an explicitly selected connection; caller owns its lifetime."""
        return connect(self.resolve(name))

    def __repr__(self):
        return f"DatabaseConfiguration(names={self.names()!r})"


def configure_databases(databases):
    """Install named database connections without opening them."""
    return DatabaseConfiguration(databases)


class DatabaseRegistry:
    """Resolve configured databases without implicit schema changes."""

    def __init__(self, configuration: DatabaseConfiguration):
        if not isinstance(configuration, DatabaseConfiguration):
            raise TypeError("configuration must be a DatabaseConfiguration")

        self._configuration = configuration
        self._instances = {}
        self._lock = RLock()
        self._closed = False

    def names(self):
        return self._configuration.names()

    def get(self, alias="default"):
        with self._lock:
            if self._closed:
                raise RuntimeError("Database registry is closed")

            self._configuration.resolve(alias)

            if alias not in self._instances:
                self._instances[alias] = self._configuration.connect(alias)

            return self._instances[alias]

    def initialized_names(self):
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
