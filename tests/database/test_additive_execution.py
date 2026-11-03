"""Fail-closed SQLite additive migration execution tests."""

import pytest

from aurora.database import (
    Column, Model, connect, migration_plan, schema_snapshot,
    initialize_additive_history, list_additive_history, execute_add_column_recorded,
    review_add_column, inspect_table,
)


class Before(Model):
    __table__ = "b2b_items"
    id = Column(int, primary_key=True)


class After(Model):
    __table__ = "b2b_items"
    id = Column(int, primary_key=True)
    note = Column(str, nullable=True)


def setup():
    conn = connect("sqlite:///:memory:")
    conn.execute_write('CREATE TABLE "b2b_items" ("id" INTEGER PRIMARY KEY)')
    conn.execute_write('INSERT INTO "b2b_items" (id) VALUES (1)')
    before = schema_snapshot(Before, dialect="sqlite")
    after = schema_snapshot(After, dialect="sqlite")
    plan = migration_plan(before, after, migration_id="m001_note")
    approval = review_add_column(conn, before, after, plan)
    return conn, before, after, plan, approval


def test_explicit_journal_and_execution():
    conn, before, after, plan, approval = setup()

    with pytest.raises(RuntimeError):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    with pytest.raises(PermissionError):
        initialize_additive_history(conn)

    initialize_additive_history(conn, confirm=True)

    with pytest.raises(PermissionError):
        execute_add_column_recorded(conn, before, after, plan, approval)

    op_id = execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)
    assert len(op_id) == 36

    assert conn.fetch_first('SELECT note FROM "b2b_items" WHERE id=1')["note"] is None

    assert list_additive_history(conn)[0].status == "applied"

    with pytest.raises(RuntimeError):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_drift_and_approval_rejected_before_journal():
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('ALTER TABLE "b2b_items" ADD COLUMN "other" INTEGER')

    with pytest.raises(RuntimeError):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert list_additive_history(conn) == ()


def test_type_drift_rejected():
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('DROP TABLE "b2b_items"')
    conn.execute_write('CREATE TABLE "b2b_items" ("id" TEXT PRIMARY KEY)')

    with pytest.raises(RuntimeError, match="type|Primary|Indexed"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert list_additive_history(conn) == ()


def test_unresolved_journal_blocks_new_ddl():
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('INSERT INTO "aurorasql_additive_history_v1" VALUES '
                       "('id1','previous','b2b_items','old','fingerprint','uncertain')")

    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_rejects_modified_approval():
    from dataclasses import replace
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)

    with pytest.raises(ValueError):
        execute_add_column_recorded(conn, before, after, plan,
                                    replace(approval, sql_preview='DROP TABLE b2b_items'), confirm=True)

    assert list_additive_history(conn) == ()


def test_post_ddl_reflection_mismatch_preserves_unresolved_journal(monkeypatch):
    """A successful ALTER is not enough to mark the operation applied."""
    from dataclasses import replace
    from aurora.database import additive_execution

    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original = additive_execution.inspect_table

    def incorrect_reflection(connection, table):
        observed = original(connection, table)

        if table == "b2b_items" and any(item.name == "note" for item in observed.columns):
            columns = tuple(
                replace(item, sql_type="INTEGER") if item.name == "note" else item
                for item in observed.columns
            )
            return replace(observed, columns=columns)

        return observed

    monkeypatch.setattr(additive_execution, "inspect_table", incorrect_reflection)

    with pytest.raises(RuntimeError, match="Post-migration column"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["started"]
    assert conn.fetch_first('SELECT note FROM "b2b_items" WHERE id=1')["note"] is None

    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_post_ddl_existing_column_drift_keeps_journal_unresolved(monkeypatch):
    """A new column cannot mask a changed existing column."""
    from dataclasses import replace
    from aurora.database import additive_execution

    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original = additive_execution.inspect_table

    def drifted_reflection(connection, table):
        observed = original(connection, table)

        if table == "b2b_items" and any(item.name == "note" for item in observed.columns):
            columns = tuple(
                replace(item, sql_type="TEXT") if item.name == "id" else item
                for item in observed.columns
            )
            return replace(observed, columns=columns)

        return observed

    monkeypatch.setattr(additive_execution, "inspect_table", drifted_reflection)

    with pytest.raises(RuntimeError, match="existing columns"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["started"]


def test_post_ddl_unsupported_table_feature_keeps_journal_unresolved(monkeypatch):
    """A new trigger after DDL must prevent a false applied-history record."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original_write = conn.execute_write

    def add_trigger_after_column(sql, params=None):
        result = original_write(sql, params)

        if sql == approval.sql_preview:
            original_write(
                'CREATE TRIGGER "b2b_items_guard" AFTER INSERT ON "b2b_items" '
                'BEGIN SELECT 1; END'
            )

        return result

    monkeypatch.setattr(conn, "execute_write", add_trigger_after_column)

    with pytest.raises(RuntimeError, match="Advanced|triggers"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["started"]


@pytest.mark.parametrize(
    ("declared", "reflected"),
    [
        ("VARCHAR(120)", "character varying(120)"),
        ("DECIMAL(12,2)", "numeric(12,2)"),
        ("TEXT", "text"),
    ],
)
def test_postgresql_supported_type_reflection(declared, reflected):
    """Canonical PostgreSQL type names must not cause false schema drift."""
    from aurora.database.additive_execution import _reflected_type_name

    assert _reflected_type_name(declared, "postgresql") == reflected
    assert _reflected_type_name(declared, "mysql") == declared.lower()


def test_ddl_reports_failure_after_execution_and_blocks_retry(monkeypatch):
    """A lost acknowledgement cannot authorize repeating a completed ALTER."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original_write = conn.execute_write

    def lose_ddl_acknowledgement(sql, params=None):
        result = original_write(sql, params)

        if sql == approval.sql_preview:
            raise ConnectionError("DDL acknowledgement lost")

        return result

    monkeypatch.setattr(conn, "execute_write", lose_ddl_acknowledgement)

    with pytest.raises(ConnectionError, match="acknowledgement"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["uncertain"]
    assert conn.fetch_first('SELECT note FROM "b2b_items" WHERE id=1')["note"] is None

    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_post_ddl_audit_write_failure_preserves_started_history(monkeypatch):
    """A failed final history update cannot be mistaken for an applied migration."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original_write = conn.execute_write

    def fail_final_audit(sql, params=None):
        if sql.startswith('UPDATE "aurorasql_additive_history_v1"') and params["status"] == "applied":
            raise OSError("audit write unavailable")

        return original_write(sql, params)

    monkeypatch.setattr(conn, "execute_write", fail_final_audit)

    with pytest.raises(OSError, match="audit write"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["started"]

    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_postgresql_type_normalization_does_not_hide_precision_drift():
    """Only spelling aliases, never semantic precision changes, are normalized."""
    from aurora.database.additive_execution import _reflected_type_name

    assert _reflected_type_name("DECIMAL(12,2)", "postgresql") != "numeric(12,4)"
    assert _reflected_type_name("VARCHAR(120)", "postgresql") != "character varying(121)"


def test_missing_intent_journal_row_blocks_ddl(monkeypatch):
    """A silently lost intent insert must never precede irreversible DDL."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original_write = conn.execute_write

    def silently_drop_intent(sql, params=None):
        if sql.startswith('INSERT INTO "aurorasql_additive_history_v1"'):
            return 0

        return original_write(sql, params)

    monkeypatch.setattr(conn, "execute_write", silently_drop_intent)

    with pytest.raises(RuntimeError, match="intent journal"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert list_additive_history(conn) == ()
    with pytest.raises(Exception):
        conn.fetch_first('SELECT note FROM "b2b_items" LIMIT 1')


def test_silent_final_audit_noop_does_not_report_success(monkeypatch):
    """A successful return from an UPDATE is not proof that history was finalized."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original_write = conn.execute_write

    def silently_skip_final_update(sql, params=None):
        if (sql.startswith('UPDATE "aurorasql_additive_history_v1"')
                and params["status"] == "applied"):
            return 0

        return original_write(sql, params)

    monkeypatch.setattr(conn, "execute_write", silently_skip_final_update)

    with pytest.raises(RuntimeError, match="Final migration journal"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["started"]
    assert conn.fetch_first('SELECT note FROM "b2b_items" WHERE id=1')["note"] is None

    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_final_audit_cannot_overwrite_concurrent_uncertain_status(monkeypatch):
    """A changed journal state must not be silently converted into success."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    original_write = conn.execute_write

    def mark_uncertain_after_ddl(sql, params=None):
        result = original_write(sql, params)

        if sql == approval.sql_preview:
            original_write(
                'UPDATE "aurorasql_additive_history_v1" SET status=:status',
                {"status": "uncertain"},
            )

        return result

    monkeypatch.setattr(conn, "execute_write", mark_uncertain_after_ddl)

    with pytest.raises(RuntimeError, match="Final migration journal"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert [entry.status for entry in list_additive_history(conn)] == ["uncertain"]

    with pytest.raises(RuntimeError, match="Unresolved"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)


def test_silent_journal_creation_noop_does_not_report_success(monkeypatch):
    """An ignored CREATE TABLE must not be reported as initialized."""
    conn, before, after, plan, approval = setup()
    original_write = conn.execute_write

    def silently_skip_journal_creation(sql, params=None):
        if sql.startswith('CREATE TABLE "aurorasql_additive_history_v1"'):
            return 0

        return original_write(sql, params)

    monkeypatch.setattr(conn, "execute_write", silently_skip_journal_creation)

    with pytest.raises(RuntimeError, match="journal creation could not be verified"):
        initialize_additive_history(conn, confirm=True)

    with pytest.raises(RuntimeError, match="journal missing"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}


def test_journal_creation_rejects_incomplete_reflected_structure(monkeypatch):
    """A visible table with missing audit fields is not a valid journal."""
    from dataclasses import replace
    from aurora.database import additive_execution

    conn, before, after, plan, approval = setup()
    original = additive_execution.inspect_table

    def incomplete_journal(connection, table):
        observed = original(connection, table)

        if table == "aurorasql_additive_history_v1" and observed.exists:
            return replace(observed, columns=observed.columns[:-1])

        return observed

    monkeypatch.setattr(additive_execution, "inspect_table", incomplete_journal)

    with pytest.raises(RuntimeError, match="structure mismatch"):
        initialize_additive_history(conn, confirm=True)

    assert conn.fetch_first(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='aurorasql_additive_history_v1'"
    ) is not None


def test_existing_journal_structure_drift_blocks_execution_before_ddl():
    """A journal altered after initialization is not safe to reuse."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('ALTER TABLE "aurorasql_additive_history_v1" RENAME TO "old_audit"')
    conn.execute_write(
        'CREATE TABLE "aurorasql_additive_history_v1" ('
        'operation_id VARCHAR(36) PRIMARY KEY, '
        'migration_id VARCHAR(80) NOT NULL, '
        'table_name VARCHAR(128) NOT NULL, '
        'column_name VARCHAR(128) NOT NULL, '
        'fingerprint VARCHAR(64) NOT NULL, '
        'status VARCHAR(16) NOT NULL)'
    )

    with pytest.raises(RuntimeError, match="structure mismatch"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    with pytest.raises(RuntimeError, match="structure mismatch"):
        list_additive_history(conn)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}


@pytest.mark.parametrize("journal_ddl", [
    'CREATE TABLE "aurorasql_additive_history_v1" ('
    'operation_id TEXT PRIMARY KEY, migration_id VARCHAR(80) NOT NULL UNIQUE, '
    'table_name VARCHAR(128) NOT NULL, column_name VARCHAR(128) NOT NULL, '
    'fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)',
    'CREATE TABLE "aurorasql_additive_history_v1" ('
    'operation_id VARCHAR(36) PRIMARY KEY, migration_id VARCHAR(80) NOT NULL UNIQUE, '
    'table_name VARCHAR(128) NOT NULL, column_name VARCHAR(128) NOT NULL, '
    "fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'started')",
])
def test_journal_semantic_drift_blocks_ddl(journal_ddl):
    """Matching column names are insufficient when audit semantics have changed."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('DROP TABLE "aurorasql_additive_history_v1"')
    conn.execute_write(journal_ddl)

    with pytest.raises(RuntimeError, match="structure mismatch"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}


def test_applied_journal_row_requires_valid_audit_evidence():
    """An altered applied row must not authorize a subsequent migration."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write(
        'INSERT INTO "aurorasql_additive_history_v1" VALUES '
        "('not-a-uuid','previous','b2b_items','old','invalid','applied')"
    )

    with pytest.raises(RuntimeError, match="journal evidence"):
        list_additive_history(conn)

    with pytest.raises(RuntimeError, match="journal evidence"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert all(column.name != "note" for column in inspect_table(conn, "b2b_items").columns)


@pytest.mark.parametrize("extra_ddl", [
    'CREATE TRIGGER audit_side_effect AFTER INSERT ON "aurorasql_additive_history_v1" '
    'BEGIN SELECT 1; END',
    'CREATE TRIGGER audit_side_effect AFTER UPDATE ON "aurorasql_additive_history_v1" '
    'BEGIN SELECT 1; END',
])
def test_journal_triggers_block_migration_before_ddl(extra_ddl):
    """Audit triggers are outside the verified journal contract."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write(extra_ddl)

    with pytest.raises(RuntimeError, match="unsupported schema features"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}


@pytest.mark.parametrize("migration_id, table_name, column_name", [
    ("invalid-id", "b2b_items", "old"),
    ("previous", "bad-table", "old"),
    ("previous", "b2b_items", "bad-column"),
])
def test_applied_history_rejects_malformed_identifiers(migration_id, table_name, column_name):
    """An applied row must use the same identifier contract as a reviewed plan."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write(
        'INSERT INTO "aurorasql_additive_history_v1" VALUES '
        '(:oid, :mid, :table, :column, :fingerprint, :status)',
        {"oid": "d9d81444-43f5-45a8-bb9a-6bd4a47b50bd", "mid": migration_id,
         "table": table_name, "column": column_name,
         "fingerprint": "a" * 64, "status": "applied"},
    )

    with pytest.raises(RuntimeError, match="journal evidence"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}


def test_journal_conflict_replace_blocks_migration_before_ddl():
    """An audit UNIQUE constraint must never silently replace older history."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('DROP TABLE "aurorasql_additive_history_v1"')
    conn.execute_write(
        'CREATE TABLE "aurorasql_additive_history_v1" ('
        'operation_id VARCHAR(36) PRIMARY KEY, '
        'migration_id VARCHAR(80) NOT NULL UNIQUE ON CONFLICT REPLACE, '
        'table_name VARCHAR(128) NOT NULL, column_name VARCHAR(128) NOT NULL, '
        'fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)'
    )

    with pytest.raises(RuntimeError, match="unsupported schema features"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}


@pytest.mark.parametrize("target", ["aurorasql_additive_history_v1", "b2b_items"])
def test_temporary_sqlite_triggers_block_additive_ddl(target):
    """TEMP triggers are invisible in sqlite_master but can change DDL semantics."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write(
        f'CREATE TEMP TRIGGER "temp_audit_guard" AFTER INSERT ON "{target}" '
        'BEGIN SELECT 1; END'
    )

    with pytest.raises(RuntimeError, match="trigger|unsupported schema features"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert "note" not in {item.name for item in inspect_table(conn, "b2b_items").columns}
    if target == "b2b_items":
        assert list_additive_history(conn) == ()


@pytest.mark.parametrize("conflict_policy", ["REPLACE", "IGNORE"])
def test_source_primary_key_conflict_policy_blocks_additive_ddl(conflict_policy):
    """A changed SQLite conflict policy is outside the approved source contract."""
    conn, before, after, plan, approval = setup()
    initialize_additive_history(conn, confirm=True)
    conn.execute_write('DROP TABLE "b2b_items"')
    conn.execute_write(
        'CREATE TABLE "b2b_items" ("id" INTEGER PRIMARY KEY '
        f'ON CONFLICT {conflict_policy})'
    )

    with pytest.raises(RuntimeError, match="Advanced schema constraints"):
        execute_add_column_recorded(conn, before, after, plan, approval, confirm=True)

    assert list_additive_history(conn) == ()
    assert "note" not in {column.name for column in inspect_table(conn, "b2b_items").columns}


@pytest.mark.parametrize("declared,reflected,accepted", [
    ("VARCHAR(120)", "character varying(120)", True),
    ("VARCHAR(120)", "character varying(121)", False),
    ("DECIMAL(12,2)", "numeric(12,2)", True),
    ("DECIMAL(12,2)", "numeric(12,4)", False),
])
def test_postgresql_post_ddl_type_equivalence(declared, reflected, accepted, monkeypatch):
    """Verify the final ADD COLUMN gate, not just the normalization helper."""
    from dataclasses import replace
    from types import SimpleNamespace
    from aurora.database import additive_execution as executor
    from aurora.database.inspection import ColumnInfo, TableInfo

    original = TableInfo(
        "items", True, (ColumnInfo("id", "bigint", False, True),),
        indexes=("items_pkey",), uniqueness_verified=True,
    )
    after_table = TableInfo(
        "items", True, (ColumnInfo("id", "bigint", False, True),
                        ColumnInfo("new_field", reflected, True, False)),
        indexes=("items_pkey",), uniqueness_verified=True,
    )
    monkeypatch.setattr(executor, "inspect_table", lambda connection, table: after_table)
    monkeypatch.setattr(executor, "_column_sql", lambda field, dialect: declared)
    before = {"tables": [{"name": "items", "columns": [{"name": "id"}]}]}
    after = {"tables": [{"name": "items", "columns": [{"name": "new_field"}]}]}
    connection = SimpleNamespace(dialect="postgresql")

    if accepted:
        executor._verify_added_column(connection, before, after, "items", "new_field", original)
    else:
        with pytest.raises(RuntimeError, match="Post-migration column differs"):
            executor._verify_added_column(connection, before, after, "items", "new_field", original)
