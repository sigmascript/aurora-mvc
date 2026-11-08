"""Read-only inventory of disposable MySQL/PostgreSQL acceptance targets.

No inventory result authorizes DDL. Existing objects and unclear permissions
must be reviewed by the team before running any acceptance migration.
"""

from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config


def inspect_server_acceptance_target(config_path, alias):
    """Fail closed unless both configured and actual identities are test-only."""
    from .cli_init import _mysql_database_identity, _postgres_database_identity
    from .history import list_history
    from .server_migration_timeline import inspect_server_timeline

    config, _ = _load_config(config_path)
    parsed = urlsplit(config.resolve(alias))
    expected_database = "aurorasql_test"
    expected_user = "aurorasql_test"

    if (parsed.scheme not in ("mysql+pymysql", "postgresql+psycopg")
            or unquote(parsed.path.lstrip("/")) != expected_database
            or unquote(parsed.username or "") != expected_user):
        raise PermissionError("Live acceptance requires the dedicated test database and user")

    with config.connect(alias) as connection:
        if connection.dialect == "mysql":
            identity = _mysql_database_identity(connection, parsed)
            auth = connection.fetch_first("SELECT CURRENT_USER() AS user_name")

            if (not auth or not isinstance(auth.get("user_name"), str)
                    or auth["user_name"].split("@", 1)[0] != expected_user):
                raise PermissionError("MySQL authenticated account is not the test user")

            rows = connection.fetch_all(
                "SELECT TABLE_NAME AS name, TABLE_TYPE AS kind "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() ORDER BY TABLE_NAME")
            owner_confirmed = False
            ownership_note = ("MySQL does not provide PostgreSQL-style database ownership; "
                              "review test account grants separately before any DDL")
        else:
            identity = _postgres_database_identity(connection, parsed)
            auth = connection.fetch_first(
                "SELECT current_user AS user_name, "
                "pg_catalog.pg_get_userbyid(d.datdba) AS owner_name "
                "FROM pg_catalog.pg_database d WHERE d.datname=current_database()")

            if (not auth or auth.get("user_name") != expected_user
                    or auth.get("owner_name") != expected_user):
                raise PermissionError("PostgreSQL test user must own the selected test database")

            schema = connection.fetch_first(
                "SELECT pg_catalog.pg_get_userbyid(n.nspowner) AS owner_name, "
                "has_schema_privilege(current_user, current_schema(), 'USAGE') AS can_use, "
                "has_schema_privilege(current_user, current_schema(), 'CREATE') AS can_create "
                "FROM pg_catalog.pg_namespace n WHERE n.nspname=current_schema()")

            if (not schema or (schema.get("owner_name") != expected_user
                               and (schema.get("can_use") is not True
                                    or schema.get("can_create") is not True))):
                raise PermissionError("PostgreSQL test user lacks verified active-schema access")

            rows = connection.fetch_all(
                "SELECT c.relname AS name, c.relkind AS kind, "
                "pg_catalog.pg_get_userbyid(c.relowner) AS owner_name "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname=current_schema() "
                "AND c.relkind IN ('r','p','v','m','f','S') ORDER BY c.relname")

            if any(row.get("owner_name") != expected_user for row in rows):
                raise PermissionError("PostgreSQL test schema contains objects owned by another user")
            owner_confirmed = True
            ownership_note = ("PostgreSQL test user owns the database and all inventoried objects; "
                              "active schema is owned or has verified USAGE and CREATE grants")

        tables = [{"name": row["name"], "kind": row["kind"]} for row in rows]
        names = [table["name"] for table in tables]

        if len(names) != len(set(names)) or any(not isinstance(name, str) for name in names):
            raise RuntimeError("Test database object inventory is inconsistent")

        if "aurorasql_migration_timeline" in names:
            timeline = inspect_server_timeline(connection, alias)
            history = [{"number": entry.number, "status": entry.status,
                        "name": entry.name} for entry in timeline]
        else:
            history = None

        if "aurorasql_schema_history" in names:
            legacy = [{"table": entry.table, "status": entry.status,
                       "dialect": entry.dialect} for entry in list_history(connection)]
        else:
            legacy = None

    return {"alias": alias, "backend": connection.dialect,
            "database_identity": identity, "authenticated_test_user": expected_user,
            "ownership_confirmed": owner_confirmed, "ownership_note": ownership_note,
            "existing_objects": tables, "native_history": history,
            "legacy_history": legacy,
            "requires_manual_review": bool(tables) or not owner_confirmed,
            "write_authorized": False}
