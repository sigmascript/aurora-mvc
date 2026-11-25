"""Guarded server table removal and mixed table transitions.

Keep parent/child foreign-key validation, target-specific destructive approval,
and MySQL uncertain-DDL handling distinct from column edits.
"""

from pathlib import Path
from uuid import uuid4


from datetime import datetime, timezone
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import sys

from aurora.core.project_policy import project_is_production

from .migration_coordination import _PG_LOCK_KEY, migration_lock
from .schema import create_table_sql, validate_init_models
from .schema_inspection import inspect_table
from .schema_planning import plan_schema
from .migration_history import history_table, read_history
from .initialization import _write_snapshot
from .schema_inspection import verify_server_declared_schema, verify_server_initial_schema
from .snapshots import schema_snapshot
from .model_discovery import model_identity_map
from .schema import _quote
from .migration_timeline import (
    complete_server_initial,
    complete_server_successor,
    inspect_server_timeline,
    mark_server_initial_uncertain,
    prepare_server_initial,
    mark_server_successor_uncertain,
    prepare_server_successor,
)


def _strip_preview(tables):
    clone = json.loads(json.dumps(tables))
    for table in clone:
        table.pop("create_sql_preview", None)
    return clone


# Guarded whole-table removals and mixed table transitions.


def single_table_removal(previous, desired):
    """Recognize one removed table with all surviving declarations unchanged."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return False

    declarations = (*previous, *desired)
    if any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in declarations
    ):
        return False

    previous_tables = _strip_preview(previous)
    desired_tables = _strip_preview(desired)

    if len(previous_tables) != len(desired_tables) + 1 or not desired_tables:
        return False

    return (
        sum(
            previous_tables[:index] + previous_tables[index + 1 :] == desired_tables
            for index in range(len(previous_tables))
        )
        == 1
    )


def mixed_table_transition(previous, desired):
    """Identify one removal plus additions without changing surviving tables."""
    if not isinstance(previous, list) or not isinstance(desired, list):
        return None

    declarations = (*previous, *desired)
    if any(
        not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in declarations
    ):
        return None

    previous_tables = _strip_preview(previous)
    desired_tables = _strip_preview(desired)
    previous_names = [table["name"] for table in previous_tables]
    desired_names = [table["name"] for table in desired_tables]

    if len(set(previous_names)) != len(previous_names) or len(set(desired_names)) != len(
        desired_names
    ):
        return None

    removed_names = set(previous_names) - set(desired_names)
    added_names = set(desired_names) - set(previous_names)

    if len(removed_names) != 1 or not added_names:
        return None

    surviving_before = [table for table in previous_tables if table["name"] not in removed_names]
    surviving_after = [table for table in desired_tables if table["name"] not in added_names]

    if not surviving_before or surviving_before != surviving_after:
        return None

    removed_name = next(iter(removed_names))
    added_in_order = [name for name in desired_names if name in added_names]

    return removed_name, added_in_order


def _table_quote(name, dialect):
    if dialect == "mysql":
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'


def _incoming_foreign_key_dependency(connection, table):
    """Detect references from surviving tables, including other MySQL schemas."""
    if connection.dialect == "mysql":
        dependency = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME=:table "
            "AND NOT (TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table) LIMIT 1",
            {"table": table},
        )
    else:
        dependency = connection.fetch_first(
            "SELECT 1 AS present FROM pg_catalog.pg_constraint c "
            "WHERE c.contype='f' AND c.confrelid=to_regclass(:table) "
            "AND c.conrelid<>c.confrelid LIMIT 1",
            {"table": table},
        )

    return dependency is not None


def _verify_removal_constraints(connection, table, outgoing_foreign_key_count):
    """Verify that a removed table owns only its primary key and known FKs."""
    if connection.dialect == "mysql":
        constraints = connection.fetch_all(
            "SELECT CONSTRAINT_TYPE AS kind FROM information_schema.TABLE_CONSTRAINTS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table",
            {"table": table},
        )
        expected = ["PRIMARY KEY"] + ["FOREIGN KEY"] * outgoing_foreign_key_count
        trigger = connection.fetch_first(
            "SELECT 1 AS present FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND EVENT_OBJECT_TABLE=:table LIMIT 1",
            {"table": table},
        )

        if trigger is not None:
            raise ValueError("Target table triggers require separate review")
    else:
        constraints = connection.fetch_all(
            "SELECT contype AS kind FROM pg_catalog.pg_constraint "
            "WHERE conrelid=to_regclass(:table)",
            {"table": table},
        )
        expected = ["p"] + ["f"] * outgoing_foreign_key_count
        metadata = connection.fetch_first(
            "SELECT c.relpersistence AS persistence, c.relispartition AS partitioned, "
            "c.relhassubclass AS inherited, c.relrowsecurity AS rls, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_trigger t WHERE t.tgrelid=c.oid "
            "AND NOT t.tgisinternal) AS triggers, "
            "(SELECT COUNT(*) FROM pg_catalog.pg_inherits h WHERE h.inhrelid=c.oid) AS parents "
            "FROM pg_catalog.pg_class c WHERE c.oid=to_regclass(:table) AND c.relkind='r'",
            {"table": table},
        )

        if (
            metadata is None
            or metadata["persistence"] != "p"
            or metadata["partitioned"]
            or metadata["inherited"]
            or metadata["rls"]
            or metadata["triggers"] != 0
            or metadata["parents"] != 0
        ):
            raise ValueError("Target table has unsupported PostgreSQL features")

    if sorted(item["kind"] for item in constraints) != sorted(expected):
        raise ValueError("Target table contains unsupported constraints")


def review_table_removal(connection, models, root, alias, identity, *, allow_new_tables=False):
    """Independently verify exact model delta, prior history, constraints, and rows."""
    dialect = connection.dialect
    if dialect not in ("mysql", "postgresql") or not identity:
        raise ValueError("Server table removal requires a verified database identity")
    ordered = validate_init_models(models, dialect=dialect, allow_extended=True)
    history, previous = read_history(connection, root, alias)
    desired = schema_snapshot(*ordered, dialect=dialect, allow_v2=True)["tables"]
    mixed = mixed_table_transition(previous["models"], desired) if allow_new_tables else None
    if mixed:
        name, added = mixed

        # A new table must not introduce a dependency on the table being removed.
        # MySQL creates new tables first and cannot roll back their DDL.
        new_declarations = {item["name"]: item for item in desired}
        for table_name in added:
            for column in new_declarations[table_name]["columns"]:
                reference = column.get("references")
                if isinstance(reference, str) and reference.split(".", 1)[0] == name:
                    raise ValueError(
                        "New table references the removed table; mixed migration refused"
                    )
    elif single_table_removal(previous["models"], desired):
        old, new = _strip_preview(previous["models"]), _strip_preview(desired)
        name = next(old[i]["name"] for i in range(len(old)) if old[:i] + old[i + 1 :] == new)
        added = []
    else:
        raise ValueError("Only one removed table with unchanged survivors is supported")

    desired_tables = _strip_preview(desired)
    observed = inspect_table(connection, name)
    expected_indexes = {"PRIMARY"} if dialect == "mysql" else {name + "_pkey"}

    # MySQL may create one supporting index per outgoing FK. A matching name
    # is not enough to prove index ownership, so validate its physical columns.
    if dialect == "mysql" and observed.foreign_keys:
        supporting_indexes = connection.fetch_all(
            "SELECT INDEX_NAME AS name, COLUMN_NAME AS column_name, "
            "SEQ_IN_INDEX AS position, NON_UNIQUE AS non_unique, "
            "INDEX_TYPE AS index_type, SUB_PART AS prefix_length "
            "FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:table "
            "AND INDEX_NAME<>'PRIMARY'",
            {"table": name},
        )
        outgoing_columns = {key[0] for key in observed.foreign_keys}
        valid_indexes = {
            item["name"]
            for item in supporting_indexes
            if item["column_name"] in outgoing_columns
            and item["position"] == 1
            and item["non_unique"] == 1
            and item["index_type"].upper() == "BTREE"
            and item["prefix_length"] is None
        }
        counts = {
            index: sum(item["name"] == index for item in supporting_indexes)
            for index in valid_indexes
        }

        if any(count != 1 for count in counts.values()):
            raise ValueError("Outgoing foreign-key indexes require separate review")

        expected_indexes.update(valid_indexes)

    if (
        not observed.exists
        or not observed.constraints_verified
        or not observed.uniqueness_verified
        or observed.foreign_key_actions is None
        or observed.unique_columns
        or set(observed.indexes) != expected_indexes
    ):
        raise ValueError(
            "Table removal requires an independently verified ordinary table "
            "without dependent objects"
        )

    if _incoming_foreign_key_dependency(connection, name):
        raise ValueError("Foreign-key dependencies from surviving tables prevent table removal")

    _verify_removal_constraints(connection, name, len(observed.foreign_keys))

    # The exact count is recorded in the review witness; any change invalidates approval.
    result = connection.fetch_first(f"SELECT COUNT(*) AS total FROM {_table_quote(name, dialect)}")
    if result is None or not isinstance(result["total"], int) or result["total"] < 0:
        raise ValueError("Cannot determine the target table row count")
    operation = {"kind": "drop_table", "table": name, "rows": result["total"]}
    if added:
        # Preserve the validated dependency-first creation order.
        operation["create_tables"] = [
            model.__table_name__ for model in ordered if model.__table_name__ in added
        ]
    digest = sha256(
        json.dumps(
            {
                "alias": alias,
                "identity": identity,
                "count": len(history),
                "physical": previous["physical_schema"],
                "operation": operation,
                "desired": desired_tables,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()
    return operation, digest, len(history)


def run_table_removal(
    args, connection, models, root, alias, identity, parsed, *, allow_new_tables=False
):
    """Double consent for populated tables, checked DDL and one forward successor."""
    from .connections import _mysql_database_identity, _postgres_database_identity
    from .migration_review import migration_title

    dialect = connection.dialect
    if project_is_production(root):
        raise PermissionError("Destructive server migrations are disabled in production")
    if (
        any(getattr(args, key, None) for key in ("apply", "approve", "backup", "name"))
        or getattr(args, "confirm", None) is not None
        or not sys.stdin.isatty()
    ):
        raise PermissionError("Table removal requires independent interactive review")
    review_options = {"allow_new_tables": True} if allow_new_tables else {}
    operation, digest, count = review_table_removal(
        connection,
        models,
        root,
        alias,
        identity,
        **review_options,
    )
    name, rows = operation["table"], operation["rows"]
    target = f"{alias}.{name}"
    print(f"AuroraSQL destructive migration — {alias} ({dialect})")
    print(f"  drop_table: {name}; stored rows: {rows}")
    if operation.get("create_tables"):
        print("  create_tables: " + ", ".join(operation["create_tables"]))
    if rows:
        print("  WARNING: This operation permanently deletes all rows in the table.")
        print("  RECOMMENDATION: Run aurora db backup separately before proceeding.")
        print("  Aurora will not automatically create or restore backups.")
    if dialect == "mysql" and operation.get("create_tables"):
        print("  WARNING: MySQL mixed DDL is not atomic. New tables are created first.")
        print("  If DDL fails, inspect physical schema and history; do not blindly retry.")
    print("  Reviewed SHA-256: " + digest)
    if input("Apply this reviewed table removal? [y/N]: ").strip().lower() != "y":
        print("Table removal cancelled; no database changes made.")
        return 0
    if rows:
        phrase = f"DELETE TABLE {target}"
        if input("Type " + phrase + " to acknowledge permanent data loss: ").strip() != phrase:
            print("Table removal cancelled; no database changes made.")
            return 0
    title = migration_title(getattr(args, "title", None), "Untitled")
    ddl_started = [False]
    identify = _mysql_database_identity if dialect == "mysql" else _postgres_database_identity

    def execute_reviewed():
        if identify(connection, parsed) != identity:
            raise RuntimeError("Database identity changed after approval")
        fresh, witness, new_count = review_table_removal(
            connection,
            models,
            root,
            alias,
            identity,
            **review_options,
        )
        if fresh != operation or new_count != count or not hmac.compare_digest(witness, digest):
            raise RuntimeError("Table-removal proposal changed after approval; no DDL executed")
        new_models = {model.__table_name__: model for model in models}

        def create_reviewed_tables():
            for table_name in operation.get("create_tables", ()):
                ddl_started[0] = True
                connection.execute_write(
                    create_table_sql(
                        new_models[table_name],
                        dialect,
                        models=models,
                    )
                )

        if dialect == "mysql":
            # MySQL implicitly commits each CREATE/DROP. Preserve existing data
            # if CREATE fails; never pretend a later failure was rolled back.
            create_reviewed_tables()

        ddl_started[0] = True
        connection.execute_write(f"DROP TABLE {_table_quote(name, dialect)}")

        if dialect == "postgresql":
            create_reviewed_tables()

        if inspect_table(connection, name).exists:
            raise RuntimeError("Dropped table still exists")
        verify_server_declared_schema(
            connection, models, history_tables={history_table(connection)}, allow_extended=True
        )
        path = _write_snapshot(connection, root, alias, models, title)
        warnings = (
            [
                f"Confirmed permanent data loss: dropped populated table {target} ({rows} rows); backup was optional"
            ]
            if rows
            else None
        )
        record = {
            "id": count + 1,
            "title": title,
            "snapshot": path.name,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "warnings": json.dumps(warnings) if warnings else None,
        }
        connection.execute_write(
            "INSERT INTO " + history_table(connection) + " (id,title,snapshot,date,warnings) "
            "VALUES (:id,:title,:snapshot,:date,:warnings)",
            record,
        )
        completed, _ = read_history(connection, root, alias)
        if len(completed) != record["id"] or any(completed[-1][k] != v for k, v in record.items()):
            raise RuntimeError("Table removal history completion failed")
        return path

    if dialect == "postgresql":
        with connection.transaction():
            # SHARE MODE prevents writes during the final review; PG transaction
            # rolls the DDL back if recording/verification fails.
            connection.execute_write(f"LOCK TABLE {_table_quote(name, dialect)} IN SHARE MODE")
            lock = connection.fetch_first(
                "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
            )
            if not lock or lock["acquired"] is not True:
                raise RuntimeError("PostgreSQL migration lock unavailable")
            path = execute_reviewed()
    else:
        with migration_lock(connection):
            # Do not retry automatically after nontransactional DDL begins.
            if identify(connection, parsed) != identity:
                raise RuntimeError("Database identity changed after approval")
            fresh, witness, new_count = review_table_removal(
                connection,
                models,
                root,
                alias,
                identity,
                **review_options,
            )
            if fresh != operation or new_count != count or not hmac.compare_digest(witness, digest):
                raise RuntimeError("Table-removal proposal changed after approval; no DDL executed")
            try:
                path = execute_reviewed()
            except Exception as exc:
                if not ddl_started[0]:
                    raise
                raise RuntimeError(
                    "MySQL table transition outcome uncertain after DDL began; "
                    "inspect physical schema and forward history before retry"
                ) from exc
    print(f"Applied Migration{count + 1} to {alias}; snapshot: {path.name}")
    return 0


# Guarded successor table creation — MySQL and PostgreSQL.
# Initial and successor server table creation are owned here.


# Single-table successor creation — MySQL.


def execute_mysql_new_table_successor(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    login_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review and execute one guarded MySQL new-table migration without retry."""
    from .cli_backup import backup_mysql_database
    from .connections import _mysql_database_identity

    if connection.dialect != "mysql":
        raise ValueError("MySQL successor requires a MySQL connection")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Server migrations require separate production approval")

    if connection._transaction_active or getattr(connection, "_migration_lock_active", False):
        raise RuntimeError("MySQL successor cannot nest locks or transactions")

    if (
        not isinstance(alias, str)
        or not alias
        or not identity
        or parsed is None
        or (backup_path is not None and (not isinstance(backup_path, str) or not backup_path))
    ):
        raise ValueError(
            "Verified database identity and valid optional backup destination are required"
        )

    archive = None
    if backup_path is not None:
        archive = backup_mysql_database(config_path, alias, backup_path, login_path)
        if not isinstance(archive, Path) or not archive.is_file():
            raise RuntimeError("MySQL successor backup has not been verified")

    operation_id = None
    prepared = False

    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != identity:
            raise RuntimeError("MySQL database identity changed after review")

        try:
            sequence = prepare_server_successor(
                connection, models, alias, identity, review, approval, name, title
            )
            prepared = True
            timeline = inspect_server_timeline(connection, alias)

            if (
                timeline is None
                or len(timeline) != sequence
                or timeline[-2].status != "applied"
                or timeline[-1].status != "prepared"
            ):
                raise RuntimeError("MySQL predecessor history changed")

            previous = timeline[-2]
            mapping = model_identity_map(models)
            added = set(mapping) - set(previous.model_tables)

            if len(added) != 1:
                raise RuntimeError("Successor requires exactly one new model")

            model = next(
                item for item in models if f"{item.__module__}:{item.__qualname__}" in added
            )
            ddl = create_table_sql(model, "mysql")
            approval_token = CreateApproval(
                model.__table_name__, "mysql", sha256(ddl.encode("utf-8")).hexdigest()
            )
            journal = _quote("aurorasql_schema_history", "mysql")
            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :table, :dialect, :fingerprint, :state)",
                {
                    "id": operation_id,
                    "table": model.__table_name__,
                    "dialect": "mysql",
                    "fingerprint": approval_token.fingerprint,
                    "state": "started",
                },
            )
            execute_create(connection, model, approval_token, confirm=True)
            affected = connection.execute_write(
                f"UPDATE {journal} SET status=:state WHERE operation_id=:id AND status=:before",
                {"state": "applied", "id": operation_id, "before": "started"},
            )

            if affected != 1:
                raise RuntimeError("MySQL successor journal completion was not confirmed")

            if sequence == 2:
                original_models = [
                    item
                    for item in models
                    if f"{item.__module__}:{item.__qualname__}" in previous.model_tables
                ]
                initial_digest = verify_server_declared_schema(
                    connection, models, evidence_models=original_models
                )

                if initial_digest != previous.schema_sha256:
                    raise RuntimeError("MySQL Initial schema changed during successor execution")

            successor_digest = verify_server_declared_schema(connection, models)
            complete_server_successor(connection, sequence, name, alias, successor_digest)

            if sequence > 2:
                from .migration_history import verify_server_applied_chain

                if not verify_server_applied_chain(connection, models, alias, identity):
                    raise RuntimeError(
                        "MySQL completed history chain failed independent verification"
                    )
        except BaseException:
            if operation_id is not None:
                try:
                    connection.execute_write(
                        f"UPDATE {_quote('aurorasql_schema_history', 'mysql')} "
                        "SET status=:state WHERE operation_id=:id "
                        "AND status IN ('started', 'applied')",
                        {"state": "uncertain", "id": operation_id},
                    )
                except BaseException:
                    # A started row is still evidence of an unknown outcome.
                    pass

            if prepared:
                try:
                    mark_server_successor_uncertain(connection, sequence, name, alias)
                except BaseException:
                    # Durable prepared intent blocks automatic replay.
                    pass

            raise

    return operation_id, archive


# Single-table successor creation — PostgreSQL.


def execute_postgres_new_table_successor(
    connection,
    models,
    *,
    alias,
    identity,
    parsed,
    config_path,
    backup_path,
    review,
    approval,
    name,
    title="Untitled",
):
    """Review, then apply one exact approved table and its journals atomically.

    PostgreSQL rollback protects the application DDL and journal on ordinary
    errors. A commit failure is never retried; the operator must reconcile.
    """
    from .cli_backup import backup_postgres_database
    from .connections import _postgres_database_identity

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL successor requires a PostgreSQL connection")

    if project_is_production(Path(config_path).expanduser().resolve().parent):
        raise PermissionError("Server migrations require separate production approval")

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL successor cannot nest transactions")

    if not isinstance(alias, str) or not alias or not identity or parsed is None:
        raise ValueError("A verified database alias and identity are required")

    # Only an explicitly supplied backup destination triggers separate artifact creation.
    archived = None
    if backup_path is not None:
        archived = backup_postgres_database(config_path, alias, backup_path)
        if not isinstance(archived, Path) or not archived.is_file():
            raise RuntimeError("PostgreSQL successor backup has not been verified")

    with connection.transaction():
        locked = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if locked is None or locked["acquired"] is not True:
            raise RuntimeError("PostgreSQL successor migration lock unavailable")

        if _postgres_database_identity(connection, parsed) != identity:
            raise RuntimeError("PostgreSQL database identity changed after review")

        sequence = prepare_server_successor(
            connection, models, alias, identity, review, approval, name, title
        )
        timeline = inspect_server_timeline(connection, alias)

        if (
            timeline is None
            or len(timeline) != sequence
            or timeline[-2].status != "applied"
            or timeline[-1].status != "prepared"
        ):
            raise RuntimeError("PostgreSQL predecessor history changed")

        previous = timeline[-2]
        mapping = model_identity_map(models)
        new_references = set(mapping) - set(previous.model_tables)

        if len(new_references) != 1:
            raise RuntimeError("Successor requires exactly one new model")

        new_reference = next(iter(new_references))
        model = next(
            item for item in models if f"{item.__module__}:{item.__qualname__}" == new_reference
        )
        ddl = create_table_sql(model, "postgresql")
        token = CreateApproval(
            model.__table_name__, "postgresql", sha256(ddl.encode("utf-8")).hexdigest()
        )
        journal = _quote("aurorasql_schema_history", "postgresql")
        operation_id = str(uuid4())
        connection.execute_write(
            f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
            "VALUES (:id, :table, :dialect, :fingerprint, :state)",
            {
                "id": operation_id,
                "table": model.__table_name__,
                "dialect": "postgresql",
                "fingerprint": token.fingerprint,
                "state": "started",
            },
        )
        execute_create(connection, model, token, confirm=True)
        affected = connection.execute_write(
            f"UPDATE {journal} SET status=:state WHERE operation_id=:id AND status=:before",
            {"state": "applied", "id": operation_id, "before": "started"},
        )

        if affected != 1:
            raise RuntimeError("PostgreSQL successor journal completion was not confirmed")

        if sequence == 2:
            original_models = [
                item
                for item in models
                if f"{item.__module__}:{item.__qualname__}" in previous.model_tables
            ]
            initial_digest = verify_server_declared_schema(
                connection, models, evidence_models=original_models
            )

            if initial_digest != previous.schema_sha256:
                raise RuntimeError("PostgreSQL Initial schema changed during successor execution")

        successor_digest = verify_server_declared_schema(connection, models)
        complete_server_successor(connection, sequence, name, alias, successor_digest)

        if sequence > 2:
            from .migration_history import verify_server_applied_chain

            if not verify_server_applied_chain(connection, models, alias, identity):
                raise RuntimeError(
                    "PostgreSQL completed history chain failed independent verification"
                )

    return operation_id, archived


# Explicit create-only approvals and guarded one-table DDL.
@dataclass(frozen=True)
class CreateApproval:
    """One-time intent to create exactly the reviewed table definition."""

    table: str
    dialect: str
    fingerprint: str


def prepare_create(connection, model):
    """Review a missing table and return its exact create-only approval token."""
    plan = plan_schema(connection, model)

    if plan.exists or len(plan.changes) != 1 or plan.changes[0].kind != "create_table":
        raise ValueError("Only missing-table creation can be approved")

    sql = plan.create_sql_preview
    return CreateApproval(plan.table, connection.dialect, sha256(sql.encode("utf-8")).hexdigest())


def execute_create(connection, model, approval, *, confirm=False):
    """Create a reviewed missing table only with explicit confirmation.

    Never retries DDL. MySQL DDL can commit implicitly. An uncertain outcome
    must be investigated by inspecting the actual database before proceeding.
    """

    if not confirm:
        raise PermissionError("Explicit confirm=True is required for schema creation")

    if not isinstance(approval, CreateApproval):
        raise TypeError("Expected a CreateApproval token")

    sql = create_table_sql(model, connection.dialect)
    expected = CreateApproval(
        model.__table_name__, connection.dialect, sha256(sql.encode("utf-8")).hexdigest()
    )

    if approval != expected:
        raise ValueError("Approval does not match the model and database dialect")

    if inspect_table(connection, model.__table_name__).exists:
        raise RuntimeError("Table already exists; refusing to modify existing schema")
    # No IF NOT EXISTS: a concurrent creator must cause an explicit DB error.
    # No catch/retry: a failed or interrupted DDL may have an uncertain outcome.
    return connection.execute_write(sql)


# Initial schema creation — MySQL and PostgreSQL.
# Guarded, distinct from successor creation and forward-only history initialization.


def execute_mysql_init(
    connection,
    models,
    *,
    approval,
    expected_approval,
    alias="default",
    expected_identity=None,
    parsed=None,
    title="Initial migration",
):
    """Create a fully approved, empty MySQL schema, preserving partial outcomes."""
    from .migration_history import initialize_history, review_history

    if connection.dialect != "mysql":
        raise ValueError("MySQL initialization requires a MySQL connection")

    if not isinstance(expected_approval, str) or len(expected_approval) != 64:
        raise ValueError("Expected MySQL approval fingerprint is required")

    if approval != expected_approval:
        raise PermissionError("Exact MySQL schema approval is required")

    if expected_identity is None or parsed is None:
        raise ValueError("Verified MySQL database identity is required")

    from .schema import validate_init_models

    ordered = validate_init_models(models, dialect="mysql")

    if list(ordered) != list(models):
        raise ValueError("Models must be in validated dependency order")

    connection._ensure_open()

    if connection._transaction_active:
        raise RuntimeError("MySQL DDL cannot execute inside a transaction")

    operation_ids = []

    with migration_lock(connection):
        from .cli_init import (
            _mysql_approval_digest,
            _mysql_database_identity,
            _mysql_read_only_preflight,
            _unsafe_warnings,
        )

        actual_identity = _mysql_database_identity(connection, parsed)

        if (
            actual_identity != expected_identity
            or _mysql_approval_digest(ordered, alias, actual_identity) != expected_approval
        ):
            raise RuntimeError("MySQL database or proposal changed after approval")

        report = _mysql_read_only_preflight(connection, ordered, alias)

        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("MySQL schema is not empty; initialization refused")

        if inspect_table(connection, "aurorasql_schema_history").exists:
            raise RuntimeError("Existing migration journal requires manual review")

        # MySQL CREATE TABLE auto-commits; this journal must persist independently.
        initialize_history(connection, confirm=True)
        journal = _quote("aurorasql_schema_history", "mysql")
        prepare_server_initial(connection, ordered, alias, expected_approval, title=title)

        try:
            for model in ordered:
                if review_history(connection):
                    raise RuntimeError("MySQL migration history requires manual review")

                if inspect_table(connection, model.__table_name__).exists:
                    raise RuntimeError("Table appeared during MySQL initialization")

                sql = create_table_sql(model, "mysql")
                token = CreateApproval(
                    model.__table_name__, "mysql", sha256(sql.encode("utf-8")).hexdigest()
                )
                operation_id = str(uuid4())
                connection.execute_write(
                    f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                    "VALUES (:id, :name, :dialect, :fingerprint, :status)",
                    {
                        "id": operation_id,
                        "name": model.__table_name__,
                        "dialect": "mysql",
                        "fingerprint": token.fingerprint,
                        "status": "started",
                    },
                )

                try:
                    execute_create(connection, model, token, confirm=True)

                    if not inspect_table(connection, model.__table_name__).exists:
                        raise RuntimeError("MySQL table creation could not be verified")

                    connection.execute_write(
                        f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                        {"status": "applied", "id": operation_id},
                    )
                except BaseException:
                    try:
                        connection.execute_write(
                            f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                            {"status": "uncertain", "id": operation_id},
                        )
                    except BaseException:
                        # 'started' remains durable evidence; never retry automatically.
                        pass

                    raise

                operation_ids.append(operation_id)

            schema_sha256 = verify_server_initial_schema(connection, ordered)
            complete_server_initial(connection, alias, schema_sha256)
        except BaseException:
            try:
                mark_server_initial_uncertain(connection, alias)
            except BaseException:
                # A durable prepared row still blocks replay if marking fails.
                pass

            raise

    return tuple(operation_ids)


def execute_postgres_init(
    connection,
    models,
    *,
    approval,
    expected_approval,
    alias="default",
    expected_identity=None,
    parsed=None,
    title="Initial migration",
):
    """Create an approved empty PostgreSQL schema atomically or raise.

    The caller must independently calculate expected_approval from the exact
    database identity, alias, and ordered DDL. This primitive does not grant
    approval, open a connection, or select a database.
    """
    from .migration_history import initialize_history

    if connection.dialect != "postgresql":
        raise ValueError("PostgreSQL initialization requires a PostgreSQL connection")

    if not models or not isinstance(models, (tuple, list)):
        raise ValueError("At least one validated model is required")

    if not isinstance(expected_approval, str) or len(expected_approval) != 64:
        raise ValueError("Expected PostgreSQL approval fingerprint is required")

    if approval != expected_approval:
        raise PermissionError("Exact PostgreSQL schema approval is required")

    from .schema import validate_init_models

    ordered = validate_init_models(models, dialect="postgresql")

    if [model.__table_name__ for model in ordered] != [model.__table_name__ for model in models]:
        raise ValueError("Models must be supplied in validated dependency order")

    connection._ensure_open()

    if connection._transaction_active:
        raise RuntimeError("PostgreSQL initialization cannot nest transactions")

    # Transaction-level advisory lock is automatically released on commit or
    # rollback. Unlike a session lock, it cannot leak into a reused connection.
    operation_ids = []

    with connection.transaction():
        locked = connection.fetch_first(
            "SELECT pg_try_advisory_xact_lock(:key) AS acquired", {"key": _PG_LOCK_KEY}
        )

        if locked is None or locked["acquired"] is not True:
            raise RuntimeError("PostgreSQL migration lock unavailable")

        if expected_identity is not None:
            from .connections import _postgres_approval_digest, _postgres_database_identity

            if (
                parsed is None
                or _postgres_database_identity(connection, parsed) != expected_identity
                or _postgres_approval_digest(ordered, alias, expected_identity) != expected_approval
            ):
                raise RuntimeError("PostgreSQL database or proposal changed after approval")

        from .cli_init import _preflight, _unsafe_warnings

        report = _preflight(connection, ordered, alias)

        if report["status"] != "EMPTY" or _unsafe_warnings(report):
            raise RuntimeError("PostgreSQL schema is not empty; initialization refused")

        if inspect_table(connection, "aurorasql_schema_history").exists:
            raise RuntimeError("Existing migration journal requires manual review")

        initialize_history(connection, confirm=True)
        prepare_server_initial(connection, ordered, alias, expected_approval, title=title)
        journal = _quote("aurorasql_schema_history", connection.dialect)

        for model in ordered:
            sql = create_table_sql(model, "postgresql")
            token = CreateApproval(
                model.__table_name__, "postgresql", sha256(sql.encode("utf-8")).hexdigest()
            )

            if inspect_table(connection, model.__table_name__).exists:
                raise RuntimeError("Table appeared during PostgreSQL initialization")

            operation_id = str(uuid4())
            connection.execute_write(
                f"INSERT INTO {journal} (operation_id, table_name, dialect, fingerprint, status) "
                "VALUES (:id, :name, :dialect, :fingerprint, :status)",
                {
                    "id": operation_id,
                    "name": model.__table_name__,
                    "dialect": "postgresql",
                    "fingerprint": token.fingerprint,
                    "status": "started",
                },
            )
            execute_create(connection, model, token, confirm=True)
            connection.execute_write(
                f"UPDATE {journal} SET status=:status WHERE operation_id=:id",
                {"status": "applied", "id": operation_id},
            )
            operation_ids.append(operation_id)

        schema_sha256 = verify_server_initial_schema(connection, ordered)
        complete_server_initial(connection, alias, schema_sha256)
    # A failed commit is uncertain: Connection.transaction raises, never returns
    # success or retries. PostgreSQL rollback removes transactional journal/DDL.
    return tuple(operation_ids)


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_mixed_table_transition(
    raw,
    root,
    db_file,
    models,
    title,
    target,
    expected_count,
    expected_witness,
    *,
    acknowledged=False,
):
    """Drop one reviewed table and create new ones in one forward transaction."""
    from .constraints import matches_models_with_indexes
    from .migration_history import (
        sqlite_new_record as _new_record,
    )
    from .migration_history import (
        sqlite_read_history as read_history,
    )
    from .migration_review import (
        UnsafeTableTransition,
        review_table_removal,
    )
    from .schema import _quote, validate_init_models

    ordered_models = validate_init_models(models, dialect="sqlite", allow_extended=True)
    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone() != (1,):
        raise RuntimeError("SQLite foreign keys must be enabled")

    snapshot_path = None

    try:
        raw.execute("BEGIN IMMEDIATE")
        _, previous = read_history(raw, root, db_file)
        old_tables = {item["name"] for item in previous["models"]}
        new_models = [model for model in ordered_models if model.__table_name__ not in old_tables]

        if not new_models:
            raise UnsafeTableTransition("Mixed table migration requires a new model table")

        actual_target, count, witness = review_table_removal(
            raw, root, db_file, models, allow_new_tables=True
        )
        if (actual_target, count, witness) != (target, expected_count, expected_witness):
            raise RuntimeError("Mixed table review is stale; run db check again")
        if count and not acknowledged:
            raise PermissionError("Populated table removal requires typed authorization")

        raw.execute(f"DROP TABLE {_quote(target, 'sqlite')}")

        for model in new_models:
            raw.execute(create_table_sql(model, "sqlite", models=ordered_models))

        matches_models_with_indexes(raw, models)
        warnings = (
            [
                f"Approved permanent deletion of table {target}: {count} existing row(s); "
                "backup recommended; not automatically created"
            ]
            if count
            else []
        )
        snapshot_path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()

    except BaseException:
        raw.rollback()
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise

    return snapshot_path


# SQLite forward-history ownership (R4.15).
def sqlite_migrate_table_removal(
    raw,
    root,
    db_file,
    models,
    title,
    target,
    expected_count,
    expected_witness,
    *,
    acknowledged=False,
):
    """Drop one reviewed table under a SQLite write lock and record its risk.

    Authorization for populated tables must be collected separately by the CLI.
    Never treat a prior empty-table observation as sufficient to permit deletion.
    """
    from .constraints import matches_models_with_indexes
    from .migration_history import sqlite_new_record as _new_record
    from .migration_review import review_table_removal
    from .schema import _quote

    raw.execute("PRAGMA foreign_keys=ON")
    if raw.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("SQLite foreign keys must be enabled")
    snapshot_path = None
    try:
        raw.execute("BEGIN IMMEDIATE")
        actual_target, count, witness = review_table_removal(raw, root, db_file, models)
        if (actual_target, count, witness) != (target, expected_count, expected_witness):
            raise RuntimeError("Table removal review is stale; run db check again")
        if count and not acknowledged:
            raise PermissionError("Populated table removal requires typed authorization")
        raw.execute(f"DROP TABLE {_quote(target, 'sqlite')}")
        matches_models_with_indexes(raw, models)
        warnings = (
            [
                f"Approved permanent deletion of table {target}: {count} existing row(s); "
                "backup recommended; not automatically created"
            ]
            if count
            else []
        )
        snapshot_path = _new_record(raw, root, db_file, models, title, warnings)
        raw.commit()
    except BaseException:
        raw.rollback()
        if snapshot_path is not None:
            snapshot_path.unlink(missing_ok=True)
        raise
    return snapshot_path
