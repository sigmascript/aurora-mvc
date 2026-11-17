"""Guarded creation of an absent configured MySQL/PostgreSQL database.

Init uses the configured credentials on an existing system bootstrap database.
No privileges are granted; uncertain DDL is never retried automatically.
"""

import re
from urllib.parse import unquote, urlsplit, urlunsplit

from .core import connect

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_PORTS = {"mysql+pymysql": 3306, "postgresql+psycopg": 5432}


class ProvisioningUncertain(RuntimeError):
    """A CREATE DATABASE attempt may have taken effect; no automatic retry."""



def is_missing_database(error, scheme):
    """Recognize *only* the documented, unambiguous missing-db driver codes."""
    kind = type(error).__module__
    if scheme == "mysql+pymysql" and kind.startswith("pymysql"):
        return bool(error.args and type(error.args[0]) is int and error.args[0] == 1049)
    if scheme == "postgresql+psycopg" and kind.startswith("psycopg"):
        return getattr(error, "sqlstate", None) == "3D000"
    return False


def is_verified_missing_postgres(error, application_url):
    """Resolve ambiguous PostgreSQL connection-startup errors without guessing.

    Some libpq/psycopg connection failures omit SQLSTATE even when the named
    database does not exist. A successful independent catalog lookup using
    exactly the configured credentials is required before treating this as a
    missing database. Never interpret known authentication/permission errors
    as missing-database evidence.
    """
    if not type(error).__module__.startswith("psycopg") or getattr(error, "sqlstate", None) is not None:
        return False
    maintenance_url, name, maintenance_db = _same_connection_bootstrap(application_url)
    if urlsplit(application_url).scheme != "postgresql+psycopg":
        return False
    try:
        with connect(maintenance_url) as connection:
            if connection.dialect != "postgresql":
                return False
            row = connection.fetch_first("SELECT current_database() AS name")
            return bool(row and row["name"] == maintenance_db and
                        not _present(connection, "postgresql+psycopg", name))
    except Exception:
        # If the independent connection fails, absence has not been proved.
        return False


def _present(connection, scheme, name):
    if scheme == "mysql+pymysql":
        row = connection.fetch_first(
            "SELECT SCHEMA_NAME AS name FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=:name",
            {"name": name},
        )
    else:
        row = connection.fetch_first(
            "SELECT datname AS name FROM pg_catalog.pg_database WHERE datname=:name",
            {"name": name},
        )
    return row is not None


def _same_connection_bootstrap(application_url):
    """Use the configured account on a known existing server database.

    No second credential or new privilege is invented. The server decides
    whether this account may CREATE DATABASE; lack of privilege fails closed.
    """
    app = urlsplit(application_url)
    if app.scheme not in _PORTS or app.query or app.fragment:
        raise ValueError("Database creation requires a supported, plain server URL")
    name = unquote(app.path.lstrip("/"))
    if not _NAME.fullmatch(name):
        raise ValueError("Database creation requires a simple ASCII database name")
    # Bootstrap databases exist on standard MySQL/PostgreSQL installations.
    bootstrap = "mysql" if app.scheme == "mysql+pymysql" else "postgres"
    if name == bootstrap:
        raise ValueError("The configured database is a reserved server bootstrap database")
    return urlunsplit((app.scheme, app.netloc, "/" + bootstrap, "", "")), name, bootstrap


def provision_database(application_url, models, *, alias, confirm):
    """Create the target once after a proven missing-db error; never retry uncertain DDL.

    Returns only after the target is created. Schema initialization is separately
    verified and executed by the existing server-forward init implementation.
    """
    import sys

    maintenance_url, name, maintenance_db = _same_connection_bootstrap(application_url)
    app = urlsplit(application_url)
    if confirm not in (None, "yes", "y"):
        raise ValueError("Database creation requires --confirm yes or an interactive review")
    if confirm is None and not sys.stdin.isatty():
        raise ValueError("Database creation needs an interactive terminal or --confirm yes")

    # Validate endpoint and target through an already-existing independent DB.
    with connect(maintenance_url) as connection:
        if connection.dialect != ("mysql" if app.scheme == "mysql+pymysql" else "postgresql"):
            raise RuntimeError("Database creation driver differs from the application database")
        if app.scheme == "mysql+pymysql":
            witness = connection.fetch_first("SELECT DATABASE() AS name, @@hostname AS server_name")
        else:
            witness = connection.fetch_first("SELECT current_database() AS name, current_user AS role")
        if not witness or witness["name"] != maintenance_db:
            raise RuntimeError("Cannot verify the selected maintenance database identity")
        if _present(connection, app.scheme, name):
            raise RuntimeError("Target database appeared during initialization review; no DDL executed")

        print(f"AuroraSQL database initialization — {alias} ({connection.dialect})")
        print(f"  create database: {name}")
        for model in models:
            print(f"  then initialize table: {model.__table_name__}")
        print("  Aurora will verify database creation before initializing its schema.")
        if confirm is None and input("Create this database and initialize its schema? [y/N]: ").strip().lower() != "y":
            print("Initialization cancelled; no database changes made.")
            return False

        # Recheck immediately before DDL; plain CREATE refuses a race instead
        # of silently adopting an unexpected database.
        if _present(connection, app.scheme, name):
            raise RuntimeError("Target database appeared before creation; no DDL executed")
        sql = (f"CREATE DATABASE `{name}`" if app.scheme == "mysql+pymysql"
               else f'CREATE DATABASE "{name}"')
        try:
            connection.execute_write(sql)
        except Exception as error:
            raise ProvisioningUncertain(
                "Database creation outcome is uncertain; inspect server catalog before retrying"
            ) from error
        # The DDL may have taken effect even if subsequent verification fails;
        # do not delete it, retry CREATE, or assume schema initialization.
        if not _present(connection, app.scheme, name):
            raise ProvisioningUncertain("Database creation outcome is uncertain; inspect server catalog before retrying")
        print(f"Created and verified database: {name}.")
    return True
