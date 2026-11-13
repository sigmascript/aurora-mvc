"""Read-only AuroraSQL initialization preflight; no schema or journal writes."""

import json
import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cli_plan import _load_config, _select_models
from .core import Connection
from .inspection import inspect_table
from .planning import plan_schema


def _preflight(connection, models, alias, *, project_root=None):
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
    plans = [plan_schema(connection, model) for model in models]
    missing = [p.table for p in plans if not p.exists]
    warnings = [warning for p in plans for warning in p.warnings]
    drift = [p.table for p in plans if p.exists and (p.changes or p.warnings)]
    unexpected = existing - target
    verified_current = False

    if dialect in ("mysql", "postgresql") and "migrations" in existing:
        from .server_forward_history import read_history
        from .snapshots import schema_snapshot
        try:
            _, latest = read_history(connection, project_root or Path.cwd(), alias)
            unexpected -= {"migrations"}
            verified_current = (not unexpected and not missing and not drift and
                                latest["models"] ==
                                schema_snapshot(*models, dialect=dialect)["tables"])
        except (RuntimeError, ValueError, OSError) as error:
            return {"database_alias": alias, "read_only": True, "status": "REFUSED",
                    "reason": str(error), "existing_tables": sorted(existing),
                    "missing_tables": missing, "unexpected_tables": sorted(unexpected),
                    "review_warnings": warnings, "model_tables": sorted(target)}
    if dialect == "sqlite" and "migrations" in existing:
        from .sqlite_forward_history import read_history, matches_models
        from .cli_plan import _load_config
        # Do not confuse a malformed or incomplete single-table history with
        # a harmless model change. Its independent schema witness must agree.
        try:
            database_path = connection._raw.execute("PRAGMA database_list").fetchall()[0][2]
            base_dir = Path(project_root) if project_root is not None else Path.cwd()
            read_history(connection._raw, base_dir, database_path)
            unexpected -= {"migrations"}
            verified_current = (not unexpected and not missing and not drift and
                                matches_models(connection._raw, models))
        except (RuntimeError, ValueError, sqlite3.Error) as error:
            return {"database_alias": alias, "read_only": True, "status": "REFUSED",
                    "reason": str(error), "existing_tables": sorted(existing),
                    "missing_tables": missing, "unexpected_tables": sorted(unexpected),
                    "review_warnings": warnings, "model_tables": sorted(target)}
    elif dialect == "sqlite" and existing:
        from .migration_timeline import _JOURNALS, inspect_native_history, verify_current_models

        # Internal history tables are never model tables. Trust their presence
        # only when the complete chain and physical schema independently agree.
        unexpected -= _JOURNALS

        if not unexpected and not missing and not drift:
            timeline, entries, findings = inspect_native_history(connection, alias)
            verified_current = (not findings and timeline is not None and
                                verify_current_models(connection._raw, models, timeline, entries))

    status = ("EMPTY" if not existing else
              "ALREADY_INITIALIZED" if verified_current else
              "REFUSED" if unexpected or missing or drift else "EXISTING_UNVERIFIED")
    return {"database_alias": alias, "read_only": True, "status": status,
            "existing_tables": sorted(existing), "missing_tables": missing,
            "unexpected_tables": sorted(unexpected), "review_warnings": warnings,
            "model_tables": sorted(target)}


def _postgres_read_only_preflight(connection, models, alias, *, project_root=None):
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
        report = _preflight(connection, models, alias, project_root=project_root)
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
    """Inspect whether initialization is safe without applying schema changes."""
    config, base_dir = _load_config(args.config)
    refs, models = _select_models(args, base_dir)
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
                report = _preflight(connection, models, args.database, project_root=base_dir)
    elif parsed.scheme == "postgresql+psycopg":
        with config.connect(args.database) as connection:
            report = _postgres_read_only_preflight(connection, models, args.database, project_root=base_dir)
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
            report = _mysql_read_only_preflight(connection, models, args.database, project_root=base_dir)

    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] in ("EMPTY", "MISSING_DATABASE", "ALREADY_INITIALIZED") else 3


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

    if row["database_name"] != unquote(parsed.path.lstrip("/")):
        raise RuntimeError("Connected PostgreSQL database differs from configuration")

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


def _mysql_read_only_preflight(connection, models, alias, *, project_root=None):
    """Inspect MySQL without writes; DDL is not transactional."""

    if connection.dialect != "mysql":
        raise ValueError("MySQL preflight requires a MySQL connection")

    report = _preflight(connection, models, alias, project_root=project_root)
    report["write_enabled"] = False
    report["transactional_ddl"] = False
    return report


def _foreign_key_violations(connection):
    """Return SQLite foreign-key violations after the complete DDL batch."""
    return connection._raw.execute("PRAGMA foreign_key_check").fetchall()


def _execute_batch(connection, models, alias="default", title="Initial migration"):
    """One migration lock, per-table journal entries, no automatic recovery.

    Caller must have performed a complete locked preflight before invoking.
    """
    from hashlib import sha256
    from uuid import uuid4

    from .coordination import migration_lock
    from .execution import CreateApproval, execute_create
    from .history import initialize_history, list_history, review_history
    from .inspection import inspect_table
    from .migration_journal_v2 import initialize_intent_journal_locked
    from .migration_timeline import complete_initial, prepare_initial
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
        report = _preflight(connection, models, alias)

        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("Database changed before locked initialization")

        initialize_history(connection, confirm=True)
        prepare_initial(connection._raw, models, alias, title)
        initialize_intent_journal_locked(connection._raw)
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

        complete_initial(connection._raw)

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


def _server_initial_decision(args, report, digest, models, dialect):
    """Review a server initialization interactively or retain explicit automation."""
    import sys
    from .migration_titles import migration_title

    eligible = report["status"] == "EMPTY" and not _unsafe_warnings(report)
    interactive = args.approve is None and args.confirm is None

    if report["status"] == "ALREADY_INITIALIZED":
        print(f"Database {args.database} is already initialized; no changes made.")
        return None, 0

    if args.approve is None and args.confirm in ("yes", "y"):
        if not eligible:
            report["write_enabled"] = False
            print(json.dumps(report, indent=2, sort_keys=True))
            return None, 3

        title = migration_title(getattr(args, "title", None) or "", "Initial migration")
        return (digest, title), None

    if interactive:
        report["approval_sha256"] = digest if eligible else None
        report["write_enabled"] = False

        if not eligible or not sys.stdin.isatty():
            print(json.dumps(report, indent=2, sort_keys=True))
            return None, 0 if eligible else 3

        print(f"AuroraSQL initialization — {args.database} ({dialect})")

        for model in models:
            print(f"  create table: {model.__table_name__}")

        if input("Initialize this database? [y/N]: ").strip().lower() != "y":
            print("Initialization cancelled; no database changes made.")
            return None, 0

        title = migration_title(getattr(args, "title", None), "Initial migration")
        return (digest, title), None

    if args.approve != digest or args.confirm != "APPLY":
        raise ValueError("Exact schema approval and --confirm APPLY are required")

    # Automation must never wait for an interactive title prompt.
    title = migration_title(getattr(args, "title", None) or "", "Initial migration")
    return (args.approve, title), None


def run_init_apply(args):
    """Initialize supported schemas after backend-specific review and approval."""
    config, base_dir = _load_config(args.config)
    refs, models = _select_models(args, base_dir)
    from .init_validation import validate_init_models
    url = config.resolve(args.database)
    parsed = urlsplit(url)

    if parsed.scheme == "postgresql+psycopg":
        models = validate_init_models(models, dialect="postgresql")

        with config.connect(args.database) as connection:
            identity = _postgres_database_identity(connection, parsed)
            digest = _postgres_approval_digest(models, args.database, identity)

            report = _postgres_read_only_preflight(connection, models, args.database, project_root=base_dir)
            decision, result = _server_initial_decision(args, report, digest, models, "PostgreSQL")

            if decision is None:
                return result

            from .server_forward_init import execute_server_forward_init
            snapshot = execute_server_forward_init(
                connection, models, base_dir, args.database, approval=decision[0],
                expected_approval=digest, expected_identity=identity,
                parsed=parsed, title=decision[1],
            )

        print(json.dumps({"status": "INITIALIZED", "database_alias": args.database,
                          "tables": [model.__table_name__ for model in models],
                          "snapshot": snapshot.name}, indent=2, sort_keys=True))
        return 0

    if parsed.scheme == "mysql+pymysql":
        models = validate_init_models(models, dialect="mysql")

        with config.connect(args.database) as connection:
            identity = _mysql_database_identity(connection, parsed)
            digest = _mysql_approval_digest(models, args.database, identity)

            report = _mysql_read_only_preflight(connection, models, args.database, project_root=base_dir)
            decision, result = _server_initial_decision(args, report, digest, models, "MySQL")

            if decision is None:
                return result

            from .server_forward_init import execute_server_forward_init
            snapshot = execute_server_forward_init(
                connection, models, base_dir, args.database, approval=decision[0],
                expected_approval=digest, expected_identity=identity,
                parsed=parsed, title=decision[1],
            )

        print(json.dumps({"status": "INITIALIZED", "database_alias": args.database,
                          "tables": [model.__table_name__ for model in models],
                          "snapshot": snapshot.name}, indent=2, sort_keys=True))
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

    if db_file.is_symlink() or (db_file.exists() and not db_file.is_file()):
        raise ValueError("SQLite initialization requires an ordinary database file")

    digest = _approval_digest(models, args.database, db_file)
    confirm_value = getattr(args, "confirm", None)
    simple_confirm = args.approve is None and confirm_value in ("yes", "y")
    interactive = args.approve is None and confirm_value is None
    create_file = not db_file.exists()

    if create_file and not db_file.parent.is_dir():
        raise ValueError("New SQLite file creation requires an existing directory")

    if create_file and not (simple_confirm or interactive):
        raise ValueError("New SQLite file creation requires approval")

    if create_file and interactive:
        import sys

        if not sys.stdin.isatty():
            raise ValueError("New SQLite database file requires an interactive terminal")

    if not create_file:
        with Connection(sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro",
                                        uri=True, isolation_level=None), "sqlite") as checked:
            preflight = _preflight(checked, models, args.database, project_root=base_dir)

        if preflight["status"] == "ALREADY_INITIALIZED":
            if args.approve is not None or (args.confirm is not None and not simple_confirm):
                raise RuntimeError("Database changed after review; initialization refused")

            print(f"Database {args.database} is already initialized; no changes made.")
            return 0

    if interactive:
        import sys

        if sys.stdin.isatty():
            if not create_file:
                with Connection(sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro",
                                                uri=True, isolation_level=None), "sqlite") as checked:
                    preflight = _preflight(checked, models, args.database, project_root=base_dir)

                if preflight["status"] != "EMPTY" or _unsafe_warnings(preflight):
                    raise RuntimeError("Initialization preflight is not eligible")

            print(f"AuroraSQL initialization — {args.database} (SQLite)")

            if create_file:
                print(f"  create database file: {db_file.resolve()}")

            for model in models:
                print(f"  create table: {model.__table_name__}")

            if input("Initialize this database? [y/N]: ").strip().lower() != "y":
                print("Initialization cancelled; no database changes made.")
                return 0

            approved_digest = digest
            approved_confirm = "APPLY"
        else:
            if getattr(args, "title", None) is not None:
                raise ValueError("Noninteractive initialization title requires explicit approval")

            approved_digest = None
            approved_confirm = None
    elif simple_confirm:
        approved_digest = digest
        approved_confirm = "APPLY"
    else:
        approved_digest = args.approve
        approved_confirm = args.confirm

    if approved_digest is None and approved_confirm is None:
        raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True,
                              isolation_level=None)

        with Connection(raw, "sqlite") as connection:
            report = _preflight(connection, models, args.database)

        report["approval_sha256"] = digest if report["status"] == "EMPTY" and not _unsafe_warnings(report) else None
        report["write_enabled"] = False
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "EMPTY" else 3

    if approved_digest != digest or approved_confirm != "APPLY":
        raise ValueError("Exact schema approval and --confirm APPLY are required")

    from .migration_titles import migration_title
    title_value = getattr(args, "title", None)

    if not interactive:
        # An explicit acknowledgement must never trigger a title prompt,
        # even when subprocess stdin inherits an interactive terminal.
        title_value = title_value or ""

    # The default title is sufficient; do not ask for a second interactive approval.
    title = migration_title(title_value or "Initial migration", "Initial migration")

    from .schema_records import prepare_schema_record
    prepare_schema_record(base_dir, db_file, models)

    if create_file:
        # Exclusive creation happens only after the human confirms both path
        # and schema. Never overwrite or implicitly create parent directories.
        with db_file.open("xb"):
            pass

    raw = sqlite3.connect(db_file.resolve().as_uri() + "?mode=rw", uri=True,
                          isolation_level=None)

    with Connection(raw, "sqlite") as connection:
        report = _preflight(connection, models, args.database)

        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("Database changed after review; initialization refused")

        from .sqlite_forward_history import initialize
        # A fresh project must use the approved single-table history; old
        # databases remain on their verified compatibility path.
        record = initialize(connection._raw, base_dir, db_file, models, title)

    print(f"Initialized {len(models)} table(s) in {db_file.name}.")
    print(f"Schema snapshot: {record}")
    return 0
