"""Offline server catalog contracts; live MySQL/PostgreSQL acceptance is separate."""

from contextlib import contextmanager

import pytest

from aurora.database import backup_catalog as catalog
from aurora.database.backup_catalog import (
    destination_fingerprint,
    list_artifacts,
    prepare_artifact,
    record_artifact,
)


class Database:
    def __init__(self, dialect):
        self.dialect = dialect
        self.created = False
        self.rows = {}
        self.ddl_count = 0
        self.sequence = 0
        self._transaction_active = False

    def fetch_all(self, statement, parameters=None):
        assert statement in (catalog._SELECT, catalog._LEGACY_SELECT)
        return list(self.rows.values())

    def execute_write(self, statement, parameters=None):
        if statement in (catalog._DDL_MYSQL, catalog._DDL_PG):
            self.created = True
            self.ddl_count += 1
        else:
            assert statement == catalog._INSERT
            self.sequence += 1
            self.rows[parameters["artifact"]] = dict(id=self.sequence, **parameters)

    def fetch_first(self, statement, parameters=None):
        assert "pg_try_advisory_xact_lock" in statement
        return {"acquired": True}

    @contextmanager
    def transaction(self):
        self._transaction_active = True
        previous = (self.created, self.ddl_count, dict(self.rows), self.sequence)
        try:
            yield self
        except Exception:
            self.created, self.ddl_count, self.rows, self.sequence = previous
            raise
        finally:
            self._transaction_active = False


def _evidence(tmp_path, backend):
    alias = "warehouse"
    uri = (
        "mysql+pymysql://tester@localhost/actual_data"
        if backend == "mysql"
        else "postgresql+psycopg://tester@localhost/actual_data"
    )
    identity = destination_fingerprint(uri)
    backup_id, path = prepare_artifact(tmp_path, alias, backend)
    path.write_bytes(b"verified hypothetical server backup")
    record_artifact(tmp_path, alias, backend, backup_id, path, "Accepted", identity)
    return alias, identity, backup_id


def _mock_catalog(monkeypatch, database):
    def exists(connection):
        assert connection is database
        return database.created

    monkeypatch.setattr(
        catalog, "_catalog_format", lambda connection: "current" if exists(connection) else None
    )

    @contextmanager
    def lock(connection):
        assert connection is database
        yield

    from aurora.database import migration_coordination as core

    monkeypatch.setattr(core, "migration_lock", lock)


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_server_catalog_records_verified_manifests_without_editing_artifacts(
    tmp_path, monkeypatch, backend
):
    alias, identity, backup_id = _evidence(tmp_path, backend)
    database = Database(backend)
    _mock_catalog(monkeypatch, database)
    before = (tmp_path / "__backups__" / alias / (backup_id + ".json")).read_bytes()
    assert (
        catalog.inspect_server_catalog(
            database, tmp_path, alias, identity, list_artifacts(tmp_path, alias, identity)
        )[0]["status"]
        == "ARTIFACT_VERIFIED"
    )
    assert not database.created
    catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert database.created and database.ddl_count == 1
    assert list(database.rows) == [backup_id + (".sql" if backend == "mysql" else ".dump")]
    catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert database.ddl_count == 1
    assert (tmp_path / "__backups__" / alias / (backup_id + ".json")).read_bytes() == before
    assert (
        catalog.inspect_server_catalog(
            database, tmp_path, alias, identity, list_artifacts(tmp_path, alias, identity)
        )[0]["status"]
        == "ARTIFACT_VERIFIED"
    )


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_catalog_rejects_mutated_rows_and_refuses_new_manifest(tmp_path, monkeypatch, backend):
    alias, identity, backup_id = _evidence(tmp_path, backend)
    database = Database(backend)
    _mock_catalog(monkeypatch, database)
    catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    database.rows[backup_id + (".sql" if backend == "mysql" else ".dump")]["title"] = "tampered"
    reports = catalog.inspect_server_catalog(
        database, tmp_path, alias, identity, list_artifacts(tmp_path, alias, identity)
    )
    assert reports[0]["status"] == "CATALOG_MISMATCH"
    with pytest.raises(ValueError, match="conflicts"):
        catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert database.ddl_count == 1


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_server_catalog_refuses_tampered_artifact_before_ddl(tmp_path, monkeypatch, backend):
    alias, identity, backup_id = _evidence(tmp_path, backend)
    database = Database(backend)
    _mock_catalog(monkeypatch, database)
    suffix = ".sql" if backend == "mysql" else ".dump"
    (tmp_path / "__backups__" / alias / (backup_id + suffix)).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="manual reconciliation"):
        catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert not database.created


def test_postgresql_catalog_transaction_rolls_back_failed_insert(tmp_path, monkeypatch):
    alias, identity, backup_id = _evidence(tmp_path, "postgresql")
    database = Database("postgresql")
    _mock_catalog(monkeypatch, database)
    original = database.execute_write

    def fail_insert(statement, parameters=None):
        if statement == catalog._INSERT:
            raise RuntimeError("simulated failure")
        return original(statement, parameters)

    database.execute_write = fail_insert
    with pytest.raises(RuntimeError, match="simulated failure"):
        catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert not database.created and database.rows == {}


def test_mysql_catalog_post_ddl_failure_is_uncertain(tmp_path, monkeypatch):
    alias, identity, backup_id = _evidence(tmp_path, "mysql")
    database = Database("mysql")
    _mock_catalog(monkeypatch, database)
    original = database.execute_write

    def fail_insert(statement, parameters=None):
        if statement == catalog._INSERT:
            raise RuntimeError("simulated failure")
        return original(statement, parameters)

    database.execute_write = fail_insert
    with pytest.raises(RuntimeError, match="outcome uncertain"):
        catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert database.created


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_catalog_rejects_nonmatching_internal_table_without_modification(monkeypatch, backend):
    from aurora.database.schema_inspection import ColumnInfo, TableInfo

    connection = Database(backend)
    columns = tuple(
        ColumnInfo(
            name,
            ("varchar(80)" if index == 0 else "text" if index in (1, 4) else "varchar(64)"),
            index in (1, 4),
            index == 0,
        )
        for index, name in enumerate(catalog._COLUMNS)
    )
    # A table with the expected names but the wrong types is not an internal catalog.
    table = TableInfo("__backups__", True, columns, indexes=("PRIMARY",), uniqueness_verified=True)
    monkeypatch.setattr(catalog, "inspect_table", lambda *_: table)
    with pytest.raises(ValueError, match="Conflicting"):
        catalog._catalog_exists(connection)


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_server_catalog_backfills_missing_verified_rows_without_read_only_writes(
    tmp_path, monkeypatch, backend
):
    alias, identity, first_id = _evidence(tmp_path, backend)
    database = Database(backend)
    _mock_catalog(monkeypatch, database)
    catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    database.rows.clear()  # Derived catalog lost a row, manifest and artifact remain intact.
    reports = catalog.inspect_server_catalog(
        database, tmp_path, alias, identity, list_artifacts(tmp_path, alias, identity)
    )
    assert len(reports) == 1 and reports[0]["status"] == "ARTIFACT_VERIFIED"
    assert not database.rows
    catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    assert list(database.rows) == [first_id + (".sql" if backend == "mysql" else ".dump")]
    assert database.ddl_count == 1


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_orphaned_server_catalog_row_is_never_silently_repaired(tmp_path, monkeypatch, backend):
    alias, identity, _ = _evidence(tmp_path, backend)
    database = Database(backend)
    _mock_catalog(monkeypatch, database)
    catalog.reconcile_server_catalog(database, tmp_path, alias, identity)
    database.rows["orphan.sql"] = dict(next(iter(database.rows.values())), artifact="orphan.sql")
    reports = catalog.inspect_server_catalog(
        database, tmp_path, alias, identity, list_artifacts(tmp_path, alias, identity)
    )
    assert any(report["status"] == "CATALOG_ORPHAN" for report in reports)
    with pytest.raises(ValueError, match="conflicts"):
        catalog.reconcile_server_catalog(database, tmp_path, alias, identity)


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_new_server_catalog_is_five_columns_and_uses_filename_as_history_key(
    tmp_path, monkeypatch, backend
):
    name, identity, backup_id = _evidence(tmp_path, backend)
    connection = Database(backend)
    _mock_catalog(monkeypatch, connection)
    catalog.reconcile_server_catalog(connection, tmp_path, name, identity)
    assert catalog._COLUMNS == ("id", "title", "artifact", "date", "warnings")
    assert "backend" not in catalog._COLUMNS
    assert "AUTO_INCREMENT" in catalog._DDL_MYSQL
    assert "BIGSERIAL" in catalog._DDL_PG
    assert len(connection.rows) == 1
    record = next(iter(connection.rows.values()))
    assert record["id"] == 1
    assert record["artifact"] == backup_id + (".sql" if backend == "mysql" else ".dump")
    assert record["date"] and record["warnings"] is None
    assert "sha256" not in record


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_legacy_server_catalog_requires_transition_not_implicit_replacement(
    tmp_path, monkeypatch, backend
):
    name, identity, backup_id = _evidence(tmp_path, backend)
    connection = Database(backend)
    connection.created = True
    monkeypatch.setattr(catalog, "_catalog_format", lambda _: "legacy")
    from aurora.database import migration_coordination as core

    @contextmanager
    def lock(_):
        yield

    monkeypatch.setattr(core, "migration_lock", lock)
    with pytest.raises(ValueError, match="explicitly reviewed transition"):
        catalog.reconcile_server_catalog(connection, tmp_path, name, identity)
    assert connection.ddl_count == 0
    assert list_artifacts(tmp_path, name, identity)[0]["status"] == "ARTIFACT_VERIFIED"


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
def test_server_five_column_schema_inspection_rejects_extra_fields(monkeypatch, backend):
    from aurora.database.schema_inspection import ColumnInfo, TableInfo

    connection = Database(backend)
    names = catalog._COLUMNS + ("backend",)
    columns = tuple(
        ColumnInfo(
            name,
            "bigint"
            if name == "id"
            else "text"
            if name in ("title", "warnings")
            else "varchar(180)"
            if name == "artifact"
            else "varchar(64)",
            name in ("title", "warnings"),
            name == "id",
        )
        for name in names
    )
    table = TableInfo(
        "__backups__",
        True,
        columns,
        indexes=("PRIMARY", "unique_artifact"),
        uniqueness_verified=True,
    )
    monkeypatch.setattr(catalog, "inspect_table", lambda *_: table)
    with pytest.raises(ValueError, match="Conflicting"):
        catalog._catalog_format(connection)
