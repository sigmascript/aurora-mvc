"""Verified, guarded, single-database JSON rename operations via six-command CLI."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from aurora.cli import main
from aurora.database.cli_explicit_operations import load_explicit_operations
from aurora.database.migration_journal_v2 import initialize_intent_journal, list_intents


def _project(tmp_path, *, related=False):
    database = tmp_path / "app.sqlite3"

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, user TEXT, visist INTEGER)')
        connection.execute('INSERT INTO users VALUES (1, ?, ?)', ('Alice', 3))

        if related:
            connection.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, user_id INTEGER '
                               'REFERENCES users(id))')

        connection.commit()
        initialize_intent_journal(connection, confirm=True)

    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///app.sqlite3"}}}), encoding="utf-8")
    return database, config


def _operations(tmp_path, *, table=False, doc=None):
    source = doc or {"format": "aurorasql.operations.v1", "database": "default"}

    if doc is None:
        if table:
            source["rename_table"] = {"users": "accounts"}
        else:
            source["rename_columns"] = {"users": {
                "user": "user_name", "visist": "visits"}}

    path = tmp_path / "renames.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    return path


def _cli(config, operations, *options):
    return main(["db", "migrate", "--config", str(config),
                 "-c", str(operations), *options])


def _preview(config, operations, capsys):
    assert _cli(config, operations, "--dry-run", "--format", "json") == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("table", [False, True])
def test_preview_then_guarded_rename_preserves_rows_and_history(tmp_path, capsys, monkeypatch, table):
    monkeypatch.delenv("AURORA_ENV", raising=False)
    database, config = _project(tmp_path)
    operations = _operations(tmp_path, table=table)
    before = database.read_bytes()
    report = _preview(config, operations, capsys)

    assert database.read_bytes() == before
    assert report["read_only"] is True
    assert report["execution_allowed"] is False
    assert report["status"] == "READY_FOR_REVIEW"
    assert report["operation"]["kind"] == ("rename_table" if table else "rename_columns")

    backup = tmp_path / "before.sqlite3"
    assert _cli(config, operations, "--apply", "--confirm", "APPLY",
                "--approve", report["review_fingerprint"], "--backup", str(backup)) == 0
    capsys.readouterr()

    assert backup.is_file()

    with sqlite3.connect(database) as connection:
        name = "accounts" if table else "users"
        row = connection.execute(f'SELECT * FROM "{name}"').fetchone()
        assert row == (1, 'Alice', 3)
        columns = [item[1] for item in connection.execute(f'PRAGMA table_info("{name}")')]
        assert columns == (["id", "user", "visist"] if table else ["id", "user_name", "visits"])
        intents = list_intents(connection)
        assert len(intents) == 1 and intents[0].state == "applied"

    with sqlite3.connect(backup) as connection:
        assert connection.execute('SELECT user,visist FROM users').fetchone() == ('Alice', 3)
        assert list_intents(connection) == ()


def test_wrong_approval_and_missing_backup_refuse_without_ddl(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AURORA_ENV", raising=False)
    database, config = _project(tmp_path)
    operations = _operations(tmp_path)
    report = _preview(config, operations, capsys)
    before = database.read_bytes()
    backup = tmp_path / "before.sqlite3"

    for extra in [("--approve", "0" * 64, "--backup", str(backup)),
                  ("--approve", report["review_fingerprint"])]:
        with pytest.raises(SystemExit) as error:
            _cli(config, operations, "--apply", "--confirm", "APPLY", *extra)

        assert error.value.code == 1
        assert database.read_bytes() == before
        assert not backup.exists()


def test_uninitialized_journal_and_dependencies_fail_closed(tmp_path, capsys):
    database, config = _project(tmp_path, related=True)
    operations = _operations(tmp_path, table=True)
    report = _preview(config, operations, capsys)
    assert report["status"] == "REFUSED"
    assert "Foreign-key dependencies require separate verification" in report["warnings"]
    assert _cli(config, operations, "--dry-run", "--check") == 3
    capsys.readouterr()

    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE aurorasql_migration_intents_v2')

    report = _preview(config, operations, capsys)
    assert report["status"] == "REFUSED"
    assert "Missing or incompatible migration intent journal" in report["warnings"]


def test_approval_is_bound_to_schema_and_database_path(tmp_path, capsys):
    database, config = _project(tmp_path)
    operations = _operations(tmp_path)
    fingerprint = _preview(config, operations, capsys)["review_fingerprint"]

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE unrelated (id INTEGER PRIMARY KEY)')

    assert _preview(config, operations, capsys)["review_fingerprint"] != fingerprint

    other = tmp_path / "second.sqlite3"
    other.write_bytes(database.read_bytes())
    second_config = tmp_path / "second_config.json"
    second_config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///second.sqlite3"}}}), encoding="utf-8")
    assert _preview(second_config, operations, capsys)["review_fingerprint"] != fingerprint


def test_production_refuses_execution_even_with_valid_approval(tmp_path, capsys, monkeypatch):
    _, config = _project(tmp_path)
    operations = _operations(tmp_path)
    approval = _preview(config, operations, capsys)["review_fingerprint"]
    monkeypatch.setenv("AURORA_ENV", "production")

    with pytest.raises(SystemExit) as error:
        _cli(config, operations, "--apply", "--confirm", "APPLY",
             "--approve", approval, "--backup", str(tmp_path / "before.sqlite3"))

    assert error.value.code == 1
    assert not (tmp_path / "before.sqlite3").exists()


@pytest.mark.parametrize("document", [
    {"format": "aurorasql.operations.v1", "database": "default", "rename_table": {"users": "users"}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_table": {"users": "users2;DROP"}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_table": {"users": "accounts"}, "drop_table": "users"},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_columns": {"users": {"user": "visist", "visist": "user"}}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_columns": {"users": {"user": "USER"}}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_columns": {"sqlite_master": {"name": "other"}}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_table": {"aurorasql_migration_intents_v2": "different"}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_table": {"AURORASQL_MIGRATION_INTENTS_V2": "different"}},
    {"format": "aurorasql.operations.v1", "database": "default", "rename_table": {"users": "accounts", "other": "else"}},
    {"format": "aurorasql.operations.v2", "database": "default", "rename_table": {"users": "accounts"}},
])
def test_invalid_json_schemas_are_rejected(tmp_path, document):
    path = _operations(tmp_path, doc=document)

    with pytest.raises(ValueError):
        load_explicit_operations(path)


def test_format_is_optional_but_review_identity_remains_normalized(tmp_path):
    operation = {"database": "default", "rename_table": {"users": "accounts"}}
    without_format = tmp_path / "simple.json"
    with_format = tmp_path / "legacy.json"
    without_format.write_text(json.dumps(operation), encoding="utf-8")
    with_format.write_text(json.dumps({"format": "aurorasql.operations.v1", **operation}),
                           encoding="utf-8")

    assert load_explicit_operations(without_format) == load_explicit_operations(with_format)


@pytest.mark.parametrize("unique", [False, True])
def test_reviewed_create_index_preserves_data_and_backup(tmp_path, capsys, monkeypatch, unique):
    monkeypatch.delenv("AURORA_ENV", raising=False)
    database, config = _project(tmp_path)
    source = _operations(tmp_path, doc={"database": "default", "create_index": {
        "table": "users", "name": "idx_users_user", "columns": ["user"],
        "unique": unique,
    }})
    report = _preview(config, source, capsys)
    assert report["status"] == "READY_FOR_REVIEW"
    assert report["operation"]["kind"] == "create_index"
    backup = tmp_path / "before.sqlite3"

    assert _cli(config, source, "--apply", "--confirm", "APPLY",
                "--approve", report["review_fingerprint"], "--backup", str(backup)) == 0
    capsys.readouterr()

    with sqlite3.connect(database) as connection:
        rows = connection.execute('SELECT user FROM users').fetchall()
        indexes = connection.execute('PRAGMA index_list("users")').fetchall()
        assert rows == [("Alice",)]
        assert any(row[1] == "idx_users_user" and row[2] == int(unique) for row in indexes)
        assert len(list_intents(connection)) == 1

    with sqlite3.connect(backup) as connection:
        assert not any(row[1] == "idx_users_user" for row in
                       connection.execute('PRAGMA index_list("users")'))


def test_unique_index_with_duplicate_rows_fails_without_source_ddl(tmp_path, capsys,
                                                                     monkeypatch):
    monkeypatch.delenv("AURORA_ENV", raising=False)
    database, config = _project(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute('INSERT INTO users VALUES (2, ?, ?)', ("Alice", 4))

    source = _operations(tmp_path, doc={"database": "default", "create_index": {
        "table": "users", "name": "idx_users_unique", "columns": ["user"],
        "unique": True,
    }})
    report = _preview(config, source, capsys)
    backup = tmp_path / "before.sqlite3"

    with pytest.raises(SystemExit) as error:
        _cli(config, source, "--apply", "--confirm", "APPLY",
             "--approve", report["review_fingerprint"], "--backup", str(backup))

    assert error.value.code == 1
    assert not backup.exists()

    with sqlite3.connect(database) as connection:
        assert not any(row[1] == "idx_users_unique" for row in
                       connection.execute('PRAGMA index_list("users")'))
        assert list_intents(connection) == ()


def test_native_timeline_refuses_unintegrated_explicit_operation(tmp_path, capsys):
    database, config = _project(tmp_path)
    source = _operations(tmp_path, doc={"database": "default", "create_index": {
        "table": "users", "name": "idx_users", "columns": ["user"],
    }})

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE aurorasql_migration_timeline (id INTEGER)')

    report = _preview(config, source, capsys)
    assert report["status"] == "REFUSED"
    assert "Native migration history is not verified" in report["warnings"]
    assert _cli(config, source, "--dry-run", "--check") == 3


def test_index_name_collision_with_existing_view_is_refused(tmp_path, capsys):
    database, config = _project(tmp_path)

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE VIEW idx_users AS SELECT id FROM users')

    source = _operations(tmp_path, doc={"database": "default", "create_index": {
        "table": "users", "name": "idx_users", "columns": ["user"],
    }})

    with pytest.raises(SystemExit) as error:
        _cli(config, source, "--dry-run", "--format", "json")

    assert error.value.code == 1


def test_rename_destination_view_name_is_refused_before_execution(tmp_path, capsys):
    database, config = _project(tmp_path)
    source = _operations(tmp_path, table=True)

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE VIEW accounts AS SELECT id FROM users')

    with pytest.raises(SystemExit) as error:
        _cli(config, source, "--dry-run", "--format", "json")

    assert error.value.code == 1


def test_native_initial_reviewed_index_appends_verified_successor(tmp_path, capsys,
                                                                    monkeypatch):
    from aurora.database import Model, column
    from aurora.database.cli_init import _execute_batch
    from aurora.database.core import Connection
    from aurora.database.migration_timeline import inspect_timeline

    class StoredMessage(Model):
        __table__ = "stored_messages"
        id = column(int, primary_key=True)
        subject = column(str)

    monkeypatch.delenv("AURORA_ENV", raising=False)
    database = tmp_path / "native.sqlite3"
    sqlite3.connect(database).close()

    with Connection(sqlite3.connect(database, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (StoredMessage,))

    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///native.sqlite3"}}}), encoding="utf-8")
    source = _operations(tmp_path, doc={"database": "default", "create_index": {
        "table": "stored_messages", "name": "idx_messages_subject", "columns": ["subject"],
    }})
    report = _preview(config, source, capsys)
    assert report["status"] == "READY_FOR_REVIEW"
    backup = tmp_path / "native_before.sqlite3"

    assert _cli(config, source, "--apply", "--confirm", "APPLY",
                "--approve", report["review_fingerprint"], "--backup", str(backup),
                "--title", "Index message subjects") == 0
    capsys.readouterr()

    with sqlite3.connect(database) as connection:
        timeline = inspect_timeline(connection, "default")
        assert [entry["number"] for entry in timeline] == [1, 2]
        assert timeline[-1]["physical_history_consistent"] is True
        assert timeline[-1]["title"] == "Index message subjects"
        assert timeline[-1]["model_tables"] == timeline[0]["model_tables"]
        assert any(row[1] == "idx_messages_subject" for row in
                   connection.execute('PRAGMA index_list("stored_messages")'))


def test_native_column_rename_appends_history_without_rewriting_initial_journal(
        tmp_path, capsys, monkeypatch):
    from aurora.database import Model, column
    from aurora.database.cli_init import _execute_batch
    from aurora.database.core import Connection
    from aurora.database.migration_timeline import inspect_timeline

    class Note(Model):
        __table__ = "review_notes"
        id = column(int, primary_key=True)
        subject = column(str)

    monkeypatch.delenv("AURORA_ENV", raising=False)
    database = tmp_path / "native_columns.sqlite3"

    with Connection(sqlite3.connect(database, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (Note,))

    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///native_columns.sqlite3"}}}), encoding="utf-8")
    source = _operations(tmp_path, doc={"database": "default", "rename_columns": {
        "review_notes": {"subject": "title"},
    }})
    report = _preview(config, source, capsys)
    assert report["status"] == "READY_FOR_REVIEW"
    backup = tmp_path / "before_columns.sqlite3"
    assert _cli(config, source, "--apply", "--confirm", "APPLY",
                "--approve", report["review_fingerprint"], "--backup", str(backup),
                "--title", "Rename note subject") == 0
    capsys.readouterr()

    with sqlite3.connect(database) as connection:
        timeline = inspect_timeline(connection, "default")
        assert [entry["number"] for entry in timeline] == [1, 2]
        assert timeline[-1]["title"] == "Rename note subject"
        assert timeline[-1]["physical_history_consistent"] is True
        assert connection.execute(
            "SELECT table_name FROM aurorasql_schema_history"
        ).fetchone() == ("review_notes",)
        assert [row[1] for row in connection.execute('PRAGMA table_info("review_notes")')] == [
            "id", "title",
        ]

    class RenamedNote(Model):
        __table__ = "review_notes"
        id = column(int, primary_key=True)
        title = column(str)

    from aurora.database.cli_history import _read_history

    with Connection(sqlite3.connect(database, isolation_level=None), "sqlite") as connection:
        verified = _read_history(connection, "default", models=(RenamedNote,))
        assert verified["timeline"][-1]["verified_current"] is True


def test_interactive_explicit_rename_requires_confirmation_and_keeps_backup(
        tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AURORA_ENV", raising=False)
    database, config = _project(tmp_path)
    source = _operations(tmp_path)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    assert _cli(config, source) == 0
    capsys.readouterr()
    assert len(list(tmp_path.glob("app.sqlite3.before-*.sqlite3"))) == 1

    with sqlite3.connect(database) as connection:
        assert "user_name" in [item[1] for item in connection.execute('PRAGMA table_info("users")')]


def test_interactive_explicit_rename_declined_without_database_changes(
        tmp_path, capsys, monkeypatch):
    database, config = _project(tmp_path)
    source = _operations(tmp_path)
    before = database.read_bytes()
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda prompt: "n")

    assert _cli(config, source) == 0
    assert "cancelled" in capsys.readouterr().out
    assert database.read_bytes() == before


@pytest.mark.parametrize("index", [
    {"table": "users", "name": "idx_users", "columns": []},
    {"table": "users", "name": "idx_users", "columns": ["user", "USER"]},
    {"table": "users", "name": "idx_users", "columns": ["user"], "unique": 1},
    {"table": "users", "name": "idx_users", "columns": ["user"], "where": "1=1"},
    {"table": "users", "name": "idx_users", "columns": ["user;DROP"]},
])
def test_create_index_rejects_invalid_or_sql_bearing_fields(tmp_path, index):
    source = _operations(tmp_path, doc={"database": "default", "create_index": index})

    with pytest.raises(ValueError):
        load_explicit_operations(source)


def test_duplicate_json_keys_are_rejected(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"format":"aurorasql.operations.v1","database":"default",'
                    '"rename_table":{"users":"accounts","users":"members"}}', encoding="utf-8")

    with pytest.raises(ValueError, match="Duplicate JSON"):
        load_explicit_operations(path)


def test_alias_conflict_and_write_flags_fail_closed(tmp_path, capsys):
    database, config = _project(tmp_path)
    operations = _operations(tmp_path)
    previous = database.read_bytes()

    with pytest.raises(SystemExit) as error:
        _cli(config, operations, "--dry-run", "--apply", "--confirm", "APPLY")

    assert error.value.code == 1
    assert database.read_bytes() == previous

    doc = json.loads(operations.read_text(encoding="utf-8"))
    doc["database"] = "other"
    operations.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(SystemExit) as error:
        _cli(config, operations, "--dry-run")

    assert error.value.code == 1
    assert database.read_bytes() == previous


def test_mysql_operation_preview_is_explicitly_refused(tmp_path, capsys):
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "mysql+pymysql://example:password@localhost/demo"}}}), encoding="utf-8")
    operations = _operations(tmp_path)
    report = _preview(config, operations, capsys)
    assert report["status"] == "REFUSED"
    assert report["execution_allowed"] is False
    assert "Backend-specific schema-operation inspection is not implemented" in report["warnings"]


def test_migration_flag_help_and_default_existing_path(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["db", "migrate", "--help"])

    assert error.value.code == 0
    text = capsys.readouterr().out
    assert "--changes" in text and "-c" in text
    assert "--config" in text
    assert "--review-file" in text


def test_recorded_table_history_requires_explicit_reconciliation(tmp_path, capsys):
    database, config = _project(tmp_path)
    operations = _operations(tmp_path, table=True)

    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE aurorasql_schema_history ('
                           'operation_id VARCHAR(36) PRIMARY KEY, '
                           'table_name VARCHAR(128) NOT NULL, dialect VARCHAR(16) NOT NULL, '
                           'fingerprint VARCHAR(64) NOT NULL, status VARCHAR(16) NOT NULL)')
        connection.execute('INSERT INTO aurorasql_schema_history VALUES (?,?,?,?,?)',
                           ('old_id', 'users', 'sqlite', '0' * 64, 'applied'))

    report = _preview(config, operations, capsys)
    assert report['status'] == 'REFUSED'
    assert 'Historical table journal requires rename-aware reconciliation' in report['warnings']


def test_changed_operation_spec_invalidates_review(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv('AURORA_ENV', raising=False)
    _, config = _project(tmp_path)
    operations = _operations(tmp_path)
    approval = _preview(config, operations, capsys)['review_fingerprint']
    document = json.loads(operations.read_text(encoding='utf-8'))
    document['rename_columns']['users']['user'] = 'display_name'
    operations.write_text(json.dumps(document), encoding='utf-8')
    backup = tmp_path / 'backup.sqlite3'

    with pytest.raises(SystemExit) as error:
        _cli(config, operations, '--apply', '--confirm', 'APPLY',
             '--approve', approval, '--backup', str(backup))

    assert error.value.code == 1
    assert not backup.exists()


def test_rename_aborts_atomically_if_catalog_verification_fails(tmp_path, capsys,
                                                                monkeypatch):
    monkeypatch.delenv('AURORA_ENV', raising=False)
    database, config = _project(tmp_path)
    operations = _operations(tmp_path)
    approval = _preview(config, operations, capsys)['review_fingerprint']
    import aurora.database.cli_explicit_operations as explicit
    real_rehearsal = explicit._rehearse_sqlite

    def invalid_rehearsal(database_path, statements):
        return real_rehearsal(database_path, statements) + [
            ('table', 'never_created', 'never_created', 'CREATE TABLE never_created(x INTEGER)')
        ]

    monkeypatch.setattr(explicit, '_rehearse_sqlite', invalid_rehearsal)
    backup = tmp_path / 'before.sqlite3'

    with pytest.raises(SystemExit) as error:
        _cli(config, operations, '--apply', '--confirm', 'APPLY',
             '--approve', approval, '--backup', str(backup))

    assert error.value.code == 1
    assert backup.exists()

    with sqlite3.connect(database) as connection:
        columns = [row[1] for row in connection.execute('PRAGMA table_info(users)')]
        assert columns == ['id', 'user', 'visist']
        assert list_intents(connection) == ()


def test_symlinked_specification_is_rejected(tmp_path):
    source = _operations(tmp_path)
    target = tmp_path / 'operation-link.json'

    try:
        target.symlink_to(source)
    except (NotImplementedError, OSError):
        pytest.skip('This platform does not allow test symlinks')

    with pytest.raises(ValueError, match='regular file'):
        load_explicit_operations(target)
