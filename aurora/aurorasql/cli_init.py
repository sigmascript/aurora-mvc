"""Read-only AuroraSQL initialization preflight; no schema or journal writes."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config, _select_models
from .core import Connection
from .inspection import inspect_table
from .planning import plan_schema


def _preflight(connection, models, alias):
    # Refuse unknown existing schema rather than silently adopting it.
    dialect = connection.dialect
    if dialect == "sqlite":
        rows = connection.fetch_all("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        existing = {row["name"] for row in rows}
    elif dialect == "mysql":
        rows = connection.fetch_all("SELECT TABLE_NAME AS name FROM information_schema.TABLES "
                                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")
        existing = {row["name"] for row in rows}
    else:
        rows = connection.fetch_all("SELECT tablename AS name FROM pg_catalog.pg_tables "
                                    "WHERE schemaname = current_schema()")
        existing = {row["name"] for row in rows}
    target = {model.__table_name__ for model in models}
    unexpected = existing - target
    plans = [plan_schema(connection, model) for model in models]
    missing = [p.table for p in plans if not p.exists]
    warnings = [warning for p in plans for warning in p.warnings]
    drift = [p.table for p in plans if p.exists and (p.changes or p.warnings)]
    # Even an apparently matching existing schema cannot be certified by this preflight.
    status = "REFUSED" if unexpected or (existing and missing) or drift else (
        "EMPTY" if not existing else "EXISTING_UNVERIFIED")
    return {"database_alias": alias, "read_only": True, "status": status,
            "existing_tables": sorted(existing), "missing_tables": missing,
            "unexpected_tables": sorted(unexpected), "review_warnings": warnings,
            "model_tables": sorted(target)}


def _postgres_read_only_preflight(connection, models, alias):
    """Inspect one PostgreSQL schema snapshot with server-enforced read-only access.

    AuroraSQL's PostgreSQL connections normally use autocommit. Explicitly
    opening a READ ONLY transaction ensures all catalog queries share a stable
    snapshot and cannot accidentally execute DDL or write journal entries.
    """
    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL preflight requires a PostgreSQL connection")
    connection._ensure_open()
    if connection._transaction_active:
        raise RuntimeError("Cannot inspect inside an active AuroraSQL transaction")
    raw = connection._raw
    raw.execute("BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    try:
        report = _preflight(connection, models, alias)
        report["transaction_mode"] = "REPEATABLE READ, READ ONLY"
        report["write_enabled"] = False
        return report
    finally:
        # Roll back even on success. Preflight must leave no open transaction.
        raw.rollback()


def _dialect_for_init_scheme(scheme):
    """Validate DDL using the actual configured engine, never SQLite by default."""
    if scheme in ("sqlite", "sqlite+pysqlite"):
        return "sqlite"
    if scheme == "postgresql+psycopg":
        return "postgresql"
    if scheme in ("mysql+pymysql", "mysql+mysqlconnector"):
        return "mysql"
    raise ValueError("Unsupported database driver for AuroraSQL initialization")


def run_init_check(args):
    config, base_dir = _load_config(args.config)
    refs, models = (_select_models(args) if base_dir is None or Path(base_dir).resolve() == Path.cwd().resolve()
                    else _select_models(args, base_dir))
    from .init_validation import validate_init_models
    url = config.resolve(args.database)
    parsed = urlsplit(url)
    dialect = _dialect_for_init_scheme(parsed.scheme)
    models = validate_init_models(models, dialect=dialect)
    if parsed.scheme in ("sqlite", "sqlite+pysqlite"):
        path = unquote(parsed.path)
        if path == "/:memory:":
            raise ValueError("Cannot inspect an in-memory database")
        name = path[2:] if path.startswith("///") else path[1:]
        db_file = Path(name).expanduser()
        if not db_file.is_absolute():
            db_file = base_dir / db_file
        if not db_file.is_file():
            report = {"database_alias": args.database, "read_only": True,
                      "status": "MISSING_DATABASE", "model_tables": sorted(m.__table_name__ for m in models)}
        else:
            raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                                  isolation_level=None)
            with Connection(raw, "sqlite") as connection:
                report = _preflight(connection, models, args.database)
    elif parsed.scheme == "postgresql+psycopg":
        with config.connect(args.database) as connection:
            report = _postgres_read_only_preflight(connection, models, args.database)
            report["write_enabled"] = False
            report["proposal_sha256"] = (
                _postgres_approval_digest(models, args.database,
                                          _postgres_database_identity(connection, parsed))
                if report["status"] == "EMPTY" and not _unsafe_warnings(report)
                else None
            )
            report["approval_sha256"] = None
    else:
        # MySQL remains read-only by convention; a dedicated permission-limited
        # account is required until its own guarded preflight is implemented.
        with config.connect(args.database) as connection:
            report = _preflight(connection, models, args.database)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in ("EMPTY", "MISSING_DATABASE") else 3


def _approval_digest(models, alias, db_path):
    """Bind the whole dependency-ordered schema and database identity."""
    from hashlib import sha256
    from .schema import create_table_sql

    reviewed = "\n".join([alias, str(db_path.resolve()),
                          *(create_table_sql(model, "sqlite") for model in models)])
    return sha256(reviewed.encode("utf-8")).hexdigest()


def _postgres_database_identity(connection, parsed):
    """Bind approval to the actual PostgreSQL database, schema, and endpoint."""
    row = connection.fetch_first(
        "SELECT current_database() AS database_name, current_schema() AS schema_name"
    )
    if not row or not row["database_name"] or not row["schema_name"]:
        raise RuntimeError("Cannot identify PostgreSQL database and schema")
    return json.dumps({
        "host": parsed.hostname.lower(),
        "port": parsed.port or 5432,
        "database": row["database_name"],
        "schema": row["schema_name"],
    }, sort_keys=True, separators=(",", ":"))


def _postgres_approval_digest(models, alias, database_identity):
    """Fingerprint an ordered PostgreSQL create-only proposal without credentials.

    This is an advisory token, NOT permission to execute DDL. The actual
    execution gate requires a separate locked, transactional implementation.
    """
    from hashlib import sha256
    from .schema import create_table_sql

    if not isinstance(database_identity, str) or not database_identity:
        raise ValueError("PostgreSQL database identity is required")
    proposal = json.dumps({
        "version": "aurorasql-postgresql-init-preview-v1",
        "alias": alias,
        "database_identity": database_identity,
        "ordered_ddl": [create_table_sql(model, "postgresql") for model in models],
    }, sort_keys=True, separators=(",", ":"))
    return sha256(proposal.encode("utf-8")).hexdigest()


def _mysql_database_identity(connection, parsed):
    """Bind review to the actual server and selected database without credentials."""
    row = connection.fetch_first(
        "SELECT DATABASE() AS database_name, @@hostname AS server_name, @@port AS server_port"
    )
    if not row or not row["database_name"]:
        raise RuntimeError("Cannot identify MySQL database")
    if row["database_name"] != unquote(parsed.path.lstrip("/")):
        raise RuntimeError("Connected MySQL database differs from configuration")
    return json.dumps({
        "host": parsed.hostname.lower(), "port": parsed.port or 3306,
        "database": row["database_name"], "server_name": row["server_name"],
        "server_port": row["server_port"],
    }, sort_keys=True, separators=(",", ":"))


def _mysql_approval_digest(models, alias, identity):
    """Fingerprint the entire ordered MySQL CREATE-only proposal."""
    from hashlib import sha256
    from .schema import create_table_sql

    proposal = json.dumps({
        "version": "aurorasql-mysql-init-v1", "alias": alias,
        "database_identity": identity,
        "ordered_ddl": [create_table_sql(model, "mysql") for model in models],
    }, sort_keys=True, separators=(",", ":"))
    return sha256(proposal.encode("utf-8")).hexdigest()


def _mysql_read_only_preflight(connection, models, alias):
    """Inspect MySQL without writes; DDL is not transactional."""
    if connection.dialect != "mysql":
        raise ValueError("MySQL preflight requires a MySQL connection")
    report = _preflight(connection, models, alias)
    report["write_enabled"] = False
    report["transactional_ddl"] = False
    return report


def _foreign_key_violations(connection):
    """Return SQLite foreign-key violations after the complete DDL batch."""
    return connection._raw.execute("PRAGMA foreign_key_check").fetchall()


def _execute_batch(connection, models):
    """One migration lock, per-table journal entries, no automatic recovery.

    Caller must have performed a complete locked preflight before invoking.
    """
    from hashlib import sha256
    from uuid import uuid4

    from .coordination import migration_lock
    from .execution import CreateApproval, execute_create
    from .history import initialize_history, list_history, review_history
    from .inspection import inspect_table
    from .schema import _quote, create_table_sql

    # SQLite PRAGMA foreign_keys cannot be changed inside a transaction.
    # Enable it before BEGIN IMMEDIATE and fail closed if it is not active.
    if connection.dialect != "sqlite":
        raise ValueError("Coordinated initialization currently supports SQLite only")
    connection._ensure_open()
    if connection._transaction_active or connection._raw.in_transaction:
        raise RuntimeError("Foreign-key initialization requires no active transaction")
    connection._raw.execute("PRAGMA foreign_keys = ON")
    if connection._raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign-key enforcement could not be enabled")

    operation_ids = []
    with migration_lock(connection):
        if inspect_table(connection, "aurorasql_schema_history").exists:
            raise RuntimeError("Existing migration journal requires manual review")
        # Revalidate under the lock, including any external schema change.
        report = _preflight(connection, models, "default")
        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("Database changed before locked initialization")
        initialize_history(connection, confirm=True)
        journal = _quote("aurorasql_schema_history", connection.dialect)
        for model in models:
            sql = create_table_sql(model, connection.dialect)
            approval = CreateApproval(model.__table_name__, connection.dialect,
                                      sha256(sql.encode("utf-8")).hexdigest())
            if review_history(connection) or list_history(connection) and any(
                entry.table == model.__table_name__ for entry in list_history(connection)
            ):
                raise RuntimeError("Migration journal requires manual review")
            if inspect_table(connection, model.__table_name__).exists:
                raise RuntimeError("Table appeared during initialization")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :name, :dialect, :fingerprint, :status)",
                {"id": operation_id, "name": model.__table_name__,
                 "dialect": connection.dialect, "fingerprint": approval.fingerprint,
                 "status": "started"})
            try:
                execute_create(connection, model, approval, confirm=True)
            except BaseException:
                try:
                    connection.execute_write(
                        f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                        {"status": "uncertain", "id": operation_id})
                except BaseException:
                    pass
                raise
            connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                {"status": "applied", "id": operation_id})
            operation_ids.append(operation_id)
        # Verify that the resulting schema has no broken FK references before
        # releasing coordination. Any failure leaves the journal for review.
        try:
            violations = _foreign_key_violations(connection)
        except BaseException:
            # A failed integrity inspection is not proof that the schema is safe.
            # Preserve uncertainty in the journal before releasing the lock.
            _mark_batch_uncertain(connection, journal, operation_ids)
            raise
        if violations:
            _mark_batch_uncertain(connection, journal, operation_ids)
            raise RuntimeError("SQLite foreign-key check failed; review migration journal")
    return operation_ids


def _mark_batch_uncertain(connection, journal, operation_ids):
    """Best-effort durable warning when post-DDL integrity cannot be certified."""
    for operation_id in operation_ids:
        try:
            connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                {"status": "uncertain", "id": operation_id})
        except BaseException:
            # Preserve the original failure. Never auto-retry this database.
            pass


def _unsafe_warnings(report):
    return [warning for warning in report["review_warnings"]
            if warning != "Preview only: creating a table requires explicit approval and execution."]


def run_init_apply(args):
    """Explicitly approved, recorded, multi-model SQLite initialization pilot."""
    config, base_dir = _load_config(args.config)
    refs, models = (_select_models(args) if base_dir is None or Path(base_dir).resolve() == Path.cwd().resolve()
                    else _select_models(args, base_dir))
    from .init_validation import validate_init_models
    url = config.resolve(args.database)
    parsed = urlsplit(url)
    if parsed.scheme == "postgresql+psycopg":
        models = validate_init_models(models, dialect="postgresql")
        with config.connect(args.database) as connection:
            identity = _postgres_database_identity(connection, parsed)
            digest = _postgres_approval_digest(models, args.database, identity)
            if args.approve is None and args.confirm is None:
                report = _postgres_read_only_preflight(connection, models, args.database)
                report["approval_sha256"] = (
                    digest if report["status"] == "EMPTY" and not _unsafe_warnings(report)
                    else None
                )
                report["write_enabled"] = False
                print(json.dumps(report, indent=2, sort_keys=True))
                return 0 if report["status"] == "EMPTY" else 3
            if args.approve != digest or args.confirm != "APPLY":
                raise ValueError("Exact schema approval and --confirm APPLY are required")
            from .postgres_init import execute_postgres_init
            operation_ids = execute_postgres_init(
                connection, models, approval=args.approve,
                expected_approval=digest, alias=args.database,
            )
        print(json.dumps({"status": "INITIALIZED", "database_alias": args.database,
                          "tables": [model.__table_name__ for model in models],
                          "operation_ids": list(operation_ids)}, indent=2, sort_keys=True))
        return 0
    if parsed.scheme == "mysql+pymysql":
        models = validate_init_models(models, dialect="mysql")
        with config.connect(args.database) as connection:
            identity = _mysql_database_identity(connection, parsed)
            digest = _mysql_approval_digest(models, args.database, identity)
            if args.approve is None and args.confirm is None:
                report = _mysql_read_only_preflight(connection, models, args.database)
                report["approval_sha256"] = (
                    digest if report["status"] == "EMPTY" and not _unsafe_warnings(report)
                    else None
                )
                print(json.dumps(report, indent=2, sort_keys=True))
                return 0 if report["status"] == "EMPTY" else 3
            if args.approve != digest or args.confirm != "APPLY":
                raise ValueError("Exact schema approval and --confirm APPLY are required")
            from .mysql_init import execute_mysql_init
            operation_ids = execute_mysql_init(
                connection, models, approval=args.approve,
                expected_approval=digest, alias=args.database,
                expected_identity=identity, parsed=parsed,
            )
        print(json.dumps({"status": "INITIALIZED", "database_alias": args.database,
                          "tables": [model.__table_name__ for model in models],
                          "operation_ids": list(operation_ids)}, indent=2, sort_keys=True))
        return 0
    if parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
        raise ValueError("Guarded initialization is not enabled for this database driver")
    models = validate_init_models(models, dialect="sqlite")
    if parsed.path == "/:memory:":
        raise ValueError("In-memory initialization is not supported")
    path = unquote(parsed.path)
    name = path[2:] if path.startswith("///") else path[1:]
    db_file = Path(name).expanduser()
    if not db_file.is_absolute():
        db_file = base_dir / db_file
    if not db_file.is_file():
        raise ValueError("Create an empty SQLite database file explicitly before initialization")
    digest = _approval_digest(models, args.database, db_file)
    if args.approve is None and args.confirm is None:
        raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                              isolation_level=None)
        with Connection(raw, "sqlite") as connection:
            report = _preflight(connection, models, args.database)
        report["approval_sha256"] = digest if report["status"] == "EMPTY" and not _unsafe_warnings(report) else None
        report["write_enabled"] = False
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "EMPTY" else 3
    if args.approve != digest or args.confirm != "APPLY":
        raise ValueError("Exact schema approval and --confirm APPLY are required")
    raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=rw", uri=True,
                          isolation_level=None)
    with Connection(raw, "sqlite") as connection:
        report = _preflight(connection, models, args.database)
        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("Database changed after review; initialization refused")
        operation_ids = _execute_batch(connection, models)
    print(json.dumps({"status": "INITIALIZED", "database_alias": args.database,
                      "tables": [model.__table_name__ for model in models],
                      "operation_ids": operation_ids}, indent=2, sort_keys=True))
    return 0
