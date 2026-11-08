"""Native server history is linear, read-only, and never self-verifying."""

import json

import pytest

from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database.server_migration_timeline import (inspect_server_timeline,
                                                       mark_server_initial_uncertain,
                                                       mark_server_successor_uncertain,
                                                       prepare_server_initial,
                                                       prepare_server_column_successor,
                                                       prepare_server_widening_successor,
                                                       prepare_server_nullability_successor,
                                                       prepare_server_default_successor,
                                                       prepare_server_successor,
                                                       complete_server_successor,
                                                       server_timeline_ddl,
                                                       verify_server_initial_current)
from aurora.database import Model, column


_COLUMNS = (
    ColumnInfo("migration_id", "BIGINT", False, True),
    ColumnInfo("name", "VARCHAR(80)", False, False),
    ColumnInfo("title", "VARCHAR(160)", False, False),
    ColumnInfo("database_alias", "VARCHAR(80)", False, False),
    ColumnInfo("review_sha256", "CHAR(64)", False, False),
    ColumnInfo("models_json", "TEXT", False, False),
    ColumnInfo("operation_json", "TEXT", True, False),
    ColumnInfo("schema_sha256", "CHAR(64)", True, False),
    ColumnInfo("state", "VARCHAR(16)", False, False),
)
_MAPPING = json.dumps({"app.models:Article": "articles"}, sort_keys=True, separators=(",", ":"))


class ServerHistoryArticle(Model):
    __table__ = "server_history_articles"
    id = column(int, primary_key=True)


class ServerHistoryComment(Model):
    __table__ = "server_history_comments"
    id = column(int, primary_key=True)


class ServerHistoryWithMemo(Model):
    __table__ = "server_history_with_memo"
    id = column(int, primary_key=True)
    memo = column(str, nullable=True)


class ServerHistoryExpandedName(Model):
    __table__ = "expanded_names"
    id = column(int, primary_key=True)
    name = column(str, length=200)


class ServerHistoryRequiredLabel(Model):
    __table__ = "required_labels"
    id = column(int, primary_key=True)
    label = column(str, length=80)


class ServerHistoryDefaultedLabel(Model):
    __table__ = "defaulted_labels"
    id = column(int, primary_key=True)
    label = column(str, length=80, default="new")


class FakeServer:
    def __init__(self, dialect, rows=None, exists=True):
        self.dialect = dialect
        self.rows = rows or []
        self.exists = exists
        self.reads = []

    def fetch_all(self, sql, params=None):
        self.reads.append(sql)
        return self.rows


class FakeWriter(FakeServer):
    def __init__(self, dialect, *, locked=False, transaction=False):
        super().__init__(dialect)
        self._migration_lock_active = locked
        self._transaction_active = transaction
        self.writes = []

    def execute_write(self, sql, params=None):
        self.writes.append((sql, params))
        return 1


def _row(number=1, name="Initial", state="applied", alias="default"):
    return {"migration_id": number, "name": name, "title": "Initial migration",
            "database_alias": alias, "review_sha256": "a" * 64,
            "models_json": _MAPPING, "schema_sha256": "b" * 64,
            "operation_json": None,
            "state": state}


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_native_server_history_read_only_and_not_verified(monkeypatch, dialect):
    from aurora.database import server_migration_timeline

    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_: TableInfo(
        "aurorasql_migration_timeline", True, _COLUMNS,
        unique_columns=("name",), uniqueness_verified=True))
    connection = FakeServer(dialect, [_row()])
    records = inspect_server_timeline(connection, "default")

    assert len(records) == 1
    assert records[0].number == 1
    assert records[0].status == "applied"
    assert records[0].model_tables == {"app.models:Article": "articles"}
    assert not records[0].physical_history_consistent
    assert not records[0].verified_current
    assert len(connection.reads) == 1
    assert connection.reads[0].startswith("SELECT ")
    assert "CREATE TABLE" in server_timeline_ddl(dialect)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
@pytest.mark.parametrize("rows", [
    [_row(number=2)],
    [_row(name="NotInitial")],
    [_row(state="uncertain"), _row(number=3, name="Next")],
    [_row(state="uncertain"), _row(number=2, name="Next")],
    [dict(_row(), state="prepared")],
    [dict(_row(), review_sha256="wrong")],
    [dict(_row(), models_json='{"untrusted": "wrong"}')],
    [dict(_row(), database_alias="other")],
])
def test_native_server_history_rejects_malformed_evidence(monkeypatch, dialect, rows):
    from aurora.database import server_migration_timeline

    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_: TableInfo(
        "aurorasql_migration_timeline", True, _COLUMNS,
        unique_columns=("name",), uniqueness_verified=True))

    with pytest.raises(RuntimeError, match="history|identity"):
        inspect_server_timeline(FakeServer(dialect, rows), "default")


def test_native_server_history_refuses_missing_unique_constraint(monkeypatch):
    from aurora.database import server_migration_timeline

    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_: TableInfo(
        "aurorasql_migration_timeline", True, _COLUMNS, uniqueness_verified=True))

    with pytest.raises(RuntimeError, match="structure"):
        inspect_server_timeline(FakeServer("mysql", [_row()]), "default")


def test_native_server_history_missing_table_is_not_auto_created(monkeypatch):
    from aurora.database import server_migration_timeline

    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_: TableInfo(
        "aurorasql_migration_timeline", False, ()))
    connection = FakeServer("postgresql")
    assert inspect_server_timeline(connection, "default") is None
    assert not connection.reads


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_native_server_history_requires_canonical_successor_operation(monkeypatch, dialect):
    from aurora.database import server_migration_timeline

    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_: TableInfo(
        "aurorasql_migration_timeline", True, _COLUMNS,
        unique_columns=("name",), uniqueness_verified=True))
    operation = '{"column":"memo","kind":"add_column","table":"articles"}'
    successor = dict(_row(number=2, name="AddMemo"), operation_json=operation)
    records = inspect_server_timeline(FakeServer(dialect, [_row(), successor]), "default")
    assert records[-1].operation == {"kind": "add_column", "table": "articles",
                                     "column": "memo"}

    for invalid in (None, '{"kind":"add_column","table":"articles"}',
                    '{"kind":"drop_table","table":"articles"}',
                    '{"table":"articles","kind":"create_table"}'):
        with pytest.raises(RuntimeError, match="operation evidence"):
            inspect_server_timeline(FakeServer(dialect, [_row(),
                                    dict(successor, operation_json=invalid)]), "default")

    widening = '{"column":"name","from":"varchar(100)","kind":"widen_column","table":"articles","to":"varchar(200)"}'
    records = inspect_server_timeline(FakeServer(dialect, [_row(),
                                    dict(successor, operation_json=widening)]), "default")
    assert records[-1].operation["from"] == "varchar(100)"

    with pytest.raises(RuntimeError, match="operation evidence"):
        inspect_server_timeline(FakeServer(dialect, [_row(), dict(
            successor, operation_json=widening.replace("varchar(200)", "DROP TABLE"))]),
            "default")

    nullability = ('{"column":"label","from":true,"kind":"change_nullability",'
                   '"table":"articles","to":false}')
    assert inspect_server_timeline(FakeServer(dialect, [_row(), dict(
        successor, operation_json=nullability)]), "default")[-1].operation["from"] is True

    with pytest.raises(RuntimeError, match="operation evidence"):
        inspect_server_timeline(FakeServer(dialect, [_row(), dict(
            successor, operation_json=nullability.replace('"to":false', '"to":true'))]),
            "default")

    default = '{"column":"label","kind":"add_default","table":"articles"}'
    assert inspect_server_timeline(FakeServer(dialect, [_row(), dict(
        successor, operation_json=default)]), "default")[-1].operation["kind"] == "add_default"


def test_public_history_shows_server_chain_but_never_claims_verified(monkeypatch):
    from aurora.database import cli_history, server_migration_timeline
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    monkeypatch.setattr(cli_history, "list_history", lambda _: ())
    monkeypatch.setattr(cli_history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             {"app.models:Article": "articles"}, "b" * 64, "applied"),
    ))

    report = cli_history._read_history(FakeServer("mysql"), "default")
    assert report["timeline"][0]["number"] == 1
    assert report["timeline"][0]["verified_current"] is False
    assert report["findings"][0]["kind"] == "server_history_not_verified"


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_server_initial_prepares_only_inside_guarded_scope(monkeypatch, dialect, locked, transaction):
    from aurora.database import server_migration_timeline

    monkeypatch.setattr(server_migration_timeline, "inspect_table", lambda *_: TableInfo(
        "aurorasql_migration_timeline", False, ()))
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)
    prepare_server_initial(connection, [ServerHistoryArticle], "default", "a" * 64)

    assert len(connection.writes) == 2
    assert connection.writes[0][0].startswith("CREATE TABLE")
    assert connection.writes[1][1]["state"] == "prepared"
    assert connection.writes[1][1]["title"] == "Initial migration"


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_initial_refuses_without_lock_or_transaction(dialect):
    connection = FakeWriter(dialect)

    with pytest.raises(RuntimeError, match="lock|transaction"):
        prepare_server_initial(connection, [ServerHistoryArticle], "default", "a" * 64)

    assert not connection.writes


def test_mysql_failure_quarantines_prepared_intent_only_under_lock():
    connection = FakeWriter("mysql", locked=True)
    mark_server_initial_uncertain(connection, "default")
    assert connection.writes[0][1] == {
        "state": "uncertain", "number": 1, "name": "Initial", "alias": "default",
        "before": "prepared",
    }

    with pytest.raises(RuntimeError, match="held MySQL"):
        mark_server_initial_uncertain(FakeWriter("mysql"), "default")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_native_initial_current_requires_matching_history_models_and_catalog(monkeypatch, dialect):
    from hashlib import sha256

    from aurora.database import history, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import create_table_sql
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    model = ServerHistoryArticle
    ddl_fingerprint = sha256(create_table_sql(model, dialect).encode("utf-8")).hexdigest()
    entry = ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                                 model_identity_map([model]), "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (entry,))
    monkeypatch.setattr(history, "list_history", lambda _: (
        HistoryEntry("operation-1", model.__table_name__, dialect, ddl_fingerprint, "applied"),))
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_initial_verification, "verify_server_initial_schema",
                        lambda *args: "b" * 64)
    connection = FakeServer(dialect)

    assert verify_server_initial_current(connection, [model], "default") is True

    monkeypatch.setattr(server_initial_verification, "verify_server_initial_schema",
                        lambda *args: "c" * 64)
    assert verify_server_initial_current(connection, [model], "default") is False

    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             model_identity_map([model]), None, "uncertain"),))
    assert verify_server_initial_current(connection, [model], "default") is False


def test_history_verified_models_can_show_server_initial_current(monkeypatch):
    from aurora.database import cli_history, server_migration_timeline
    from aurora.database.server_migration_timeline import ServerMigrationEntry

    monkeypatch.setattr(cli_history, "list_history", lambda _: ())
    monkeypatch.setattr(cli_history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             {"app.models:Article": "articles"}, "b" * 64, "applied"),))
    monkeypatch.setattr(server_migration_timeline, "verify_server_initial_current",
                        lambda *args: True)
    report = cli_history._read_history(FakeServer("postgresql"), "default",
                                       models=[ServerHistoryArticle])
    assert report["timeline"][0]["verified_current"] is True
    assert not report["findings"]


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_server_successor_intent_is_isolated_by_database_alias(monkeypatch, dialect,
                                                                locked, transaction):
    from aurora.database import server_migration_timeline, server_model_review
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map([ServerHistoryArticle]),
                                   "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (initial,))
    monkeypatch.setattr(server_migration_timeline, "verify_server_initial_current",
                        lambda conn, models, alias: alias == "accounting")
    proposal = ServerModelProposal(
        dialect, ("CREATE TABLE server_history_comments",),
        ({"kind": "create_table", "table": "server_history_comments"},), "c" * 64)
    monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *args: proposal)
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)
    models = [ServerHistoryArticle, ServerHistoryComment]

    assert prepare_server_successor(connection, models, "accounting", "accounting-db",
                                    proposal, "c" * 64, "AddComments") == 2
    assert connection.writes[0][1]["alias"] == "accounting"
    assert connection.writes[0][1]["number"] == 2
    assert connection.writes[0][1]["state"] == "prepared"

    connection.writes.clear()

    with pytest.raises(RuntimeError, match="Historical"):
        prepare_server_successor(connection, models, "default", "accounting-db",
                                 proposal, "c" * 64, "AddComments")

    assert not connection.writes


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_server_successor_completion_is_conditional_and_alias_bound(dialect, locked, transaction):
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)
    complete_server_successor(connection, 2, "AddComments", "accounting", "b" * 64)
    assert connection.writes[0][1] == {
        "schema": "b" * 64, "state": "applied", "number": 2,
        "name": "AddComments", "alias": "accounting", "before": "prepared",
    }

    if dialect == "mysql":
        mark_server_successor_uncertain(connection, 2, "AddComments", "accounting")
        assert connection.writes[1][1]["state"] == "uncertain"
        assert connection.writes[1][1]["before"] == "prepared"


def test_server_successor_refuses_unlocked_writes():
    connection = FakeWriter("mysql")

    with pytest.raises(RuntimeError, match="lock"):
        complete_server_successor(connection, 2, "AddComments", "accounting", "b" * 64)

    with pytest.raises(RuntimeError, match="lock"):
        mark_server_successor_uncertain(connection, 2, "AddComments", "accounting")

    assert not connection.writes


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_server_column_successor_is_alias_bound_and_requires_historical_proof(
        monkeypatch, dialect, locked, transaction):
    from aurora.database import (server_column_history, server_migration_timeline,
                                 server_model_review)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    mapping = model_identity_map([ServerHistoryArticle])
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, mapping, "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *args: (initial,))
    review = ServerModelProposal(dialect, ("ALTER TABLE server_history_articles ADD memo TEXT",),
                                 ({"kind": "add_column", "table": "server_history_articles",
                                   "column": "memo"},), "c" * 64)
    monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *args: review)
    proven = []
    monkeypatch.setattr(server_column_history, "verify_server_column_addition_predecessor",
                        lambda *args: proven.append(args) or ())
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)

    assert prepare_server_column_successor(connection, [ServerHistoryArticle],
                                           "accounting", "accounting-db", review,
                                           "c" * 64, "AddMemo") == 2
    assert proven
    assert connection.writes[0][1]["alias"] == "accounting"
    assert json.loads(connection.writes[0][1]["operation"])["kind"] == "add_column"

    connection.writes.clear()

    with pytest.raises(PermissionError):
        prepare_server_column_successor(connection, [ServerHistoryArticle],
                                        "accounting", "accounting-db", review,
                                        "d" * 64, "AddMemo")

    assert not connection.writes


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_widening_successor_requires_review_and_historical_proof(monkeypatch, dialect,
                                                                 locked, transaction):
    from aurora.database import (server_column_widening, server_migration_timeline,
                                 server_widening_history)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    models = [ServerHistoryWithMemo]
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map(models), "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (initial,))
    proposal = ServerModelProposal(dialect, ("ALTER TABLE table ALTER COLUMN",),
                                   ({"kind": "widen_column", "table": models[0].__table_name__,
                                     "column": "memo", "from": "varchar(50)",
                                     "to": "varchar(100)"},), "c" * 64)
    monkeypatch.setattr(server_column_widening, "review_server_column_widening",
                        lambda *args: proposal)
    proven = []
    monkeypatch.setattr(server_widening_history, "verify_server_widening_predecessor",
                        lambda *args: proven.append(args) or ())
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)
    assert prepare_server_widening_successor(connection, models, "accounting", "database-id",
                                             proposal, "c" * 64, "WidenMemo") == 2
    assert proven
    assert json.loads(connection.writes[0][1]["operation"])["kind"] == "widen_column"

    connection.writes.clear()

    with pytest.raises(PermissionError):
        prepare_server_widening_successor(connection, models, "accounting", "database-id",
                                          proposal, "d" * 64, "WidenMemo")

    assert not connection.writes


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_nullability_successor_requires_exact_review_under_lock(monkeypatch, dialect,
                                                                  locked, transaction):
    from aurora.database import (server_nullability_review, server_nullability_history,
                                 server_migration_timeline)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    models = [ServerHistoryRequiredLabel]
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map(models), "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (initial,))
    proposal = ServerModelProposal(dialect, ("ALTER TABLE required_labels",),
                                   ({"kind": "change_nullability", "table": "required_labels",
                                     "column": "label", "from": True, "to": False},), "c" * 64)
    reviewed = []
    monkeypatch.setattr(server_nullability_review, "review_server_nullability_change",
                        lambda *args: reviewed.append(True) or proposal)
    proven = []
    monkeypatch.setattr(server_nullability_history, "verify_server_nullability_predecessor",
                        lambda *args: proven.append(True))
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)
    assert prepare_server_nullability_successor(connection, models, "accounting", "identity",
                                                proposal, "c" * 64, "RequireLabel") == 2
    assert reviewed and proven
    assert json.loads(connection.writes[0][1]["operation"])["from"] is True
    connection.writes.clear()

    with pytest.raises(PermissionError):
        prepare_server_nullability_successor(connection, models, "accounting", "identity",
                                             proposal, "d" * 64, "RequireLabel")

    assert not connection.writes


@pytest.mark.parametrize("dialect,locked,transaction", [
    ("mysql", True, False), ("postgresql", False, True),
])
def test_default_successor_requires_historical_proof_under_lock(monkeypatch, dialect,
                                                                 locked, transaction):
    from aurora.database import (server_default_review, server_default_history,
                                 server_migration_timeline)
    from aurora.database.model_identity import model_identity_map
    from aurora.database.server_migration_timeline import ServerMigrationEntry
    from aurora.database.server_model_review import ServerModelProposal

    models = [ServerHistoryDefaultedLabel]
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map(models), "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: (initial,))
    proposal = ServerModelProposal(dialect, ("ALTER TABLE defaulted_labels",),
                                   ({"kind": "add_default", "table": "defaulted_labels",
                                     "column": "label"},), "c" * 64)
    monkeypatch.setattr(server_default_review, "review_server_default_addition",
                        lambda *args: proposal)
    proven = []
    monkeypatch.setattr(server_default_history, "verify_server_default_predecessor",
                        lambda *args: proven.append(True))
    connection = FakeWriter(dialect, locked=locked, transaction=transaction)
    assert prepare_server_default_successor(connection, models, "accounting", "identity",
                                            proposal, "c" * 64, "DefaultLabel") == 2
    assert proven
    assert json.loads(connection.writes[0][1]["operation"])["kind"] == "add_default"
    connection.writes.clear()

    with pytest.raises(PermissionError):
        prepare_server_default_successor(connection, models, "accounting", "identity",
                                         proposal, "d" * 64, "DefaultLabel")

    assert not connection.writes


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_initial_does_not_accept_duplicate_journal_table_as_missing_model(monkeypatch,
                                                                                 dialect):
    from hashlib import sha256

    from aurora.database import history, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import create_table_sql
    from aurora.database.server_migration_timeline import (ServerMigrationEntry,
                                                           verify_server_initial_current)

    models = [ServerHistoryArticle, ServerHistoryComment]
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, model_identity_map(models),
                                   "b" * 64, "applied")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *_: (initial,))
    fingerprint = sha256(create_table_sql(models[0], dialect).encode()).hexdigest()
    monkeypatch.setattr(history, "list_history", lambda _: (
        HistoryEntry("first", models[0].__table_name__, dialect, fingerprint, "applied"),
        HistoryEntry("second", models[0].__table_name__, dialect, fingerprint, "applied"),
    ))
    # Even if a weaker journal reviewer misses duplicates, the native gate
    # must independently require a one-to-one table mapping.
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())

    assert verify_server_initial_current(FakeServer(dialect), models, "accounting") is False


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_successor_verifies_both_historical_and_current_schema(monkeypatch, dialect):
    from hashlib import sha256

    from aurora.database import history, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import create_table_sql
    from aurora.database.server_migration_timeline import (ServerMigrationEntry,
                                                           verify_server_successor_current)

    models = [ServerHistoryArticle, ServerHistoryComment]
    alias = "accounting"
    original = model_identity_map(models[:1])
    current = model_identity_map(models)
    timeline = (
        ServerMigrationEntry(1, "Initial", "Initial migration", alias, "a" * 64,
                             original, "b" * 64, "applied"),
        ServerMigrationEntry(2, "AddComments", "Untitled", alias, "c" * 64,
                             current, "d" * 64, "applied",
                             operation={"kind": "create_table", "table": "server_history_comments"}),
    )
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda connection, selected_alias: timeline if selected_alias == alias else None)
    monkeypatch.setattr(history, "list_history", lambda _: tuple(
        HistoryEntry(str(index), model.__table_name__, dialect,
                     sha256(create_table_sql(model, dialect).encode()).hexdigest(), "applied")
        for index, model in enumerate(models)
    ))
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())
    observed = {"original": "b" * 64, "current": "d" * 64}

    def verify_schema(connection, selected, *, evidence_models=None):
        assert selected == models
        return observed["original" if evidence_models is not None else "current"]

    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema", verify_schema)
    connection = FakeServer(dialect)
    assert verify_server_successor_current(connection, models, alias)
    assert not verify_server_successor_current(connection, models, "default")

    observed["original"] = "e" * 64
    assert not verify_server_successor_current(connection, models, alias)
    observed["original"] = "b" * 64
    observed["current"] = "e" * 64
    assert not verify_server_successor_current(connection, models, alias)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_applied_column_successor_verifies_original_and_alter_journals(monkeypatch, dialect):
    from hashlib import sha256

    from aurora.database import history, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import create_table_sql
    from aurora.database.server_column_history import column_addition_predecessors
    from aurora.database.server_model_review import _add_column_statement
    from aurora.database.server_migration_timeline import (ServerMigrationEntry,
                                                           verify_server_successor_current)

    model = ServerHistoryWithMemo
    operation = {"kind": "add_column", "table": model.__table_name__, "column": "memo"}
    previous = column_addition_predecessors([model], operation)[0]
    original = sha256(create_table_sql(previous, dialect).encode()).hexdigest()
    alter = sha256(_add_column_statement(model, "memo", dialect).encode()).hexdigest()
    mapping = model_identity_map([model])
    initial = ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                   "a" * 64, mapping, "b" * 64, "applied")
    successor = ServerMigrationEntry(2, "AddMemo", "Untitled", "accounting",
                                     "c" * 64, mapping, "d" * 64, "applied",
                                     operation=operation)
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *args: (initial, successor))
    entries = [HistoryEntry("initial", model.__table_name__, dialect, original, "applied"),
               HistoryEntry("alter", model.__table_name__, dialect, alter, "applied")]
    monkeypatch.setattr(history, "list_history", lambda _: tuple(entries))
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())
    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema",
                        lambda conn, models, **kwargs: ("d" * 64, "b" * 64))
    connection = FakeServer(dialect)

    assert verify_server_successor_current(connection, [model], "accounting")
    entries[-1] = HistoryEntry("alter", model.__table_name__, dialect, "e" * 64, "applied")
    assert not verify_server_successor_current(connection, [model], "accounting")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_applied_widening_requires_original_type_and_exact_alter_evidence(monkeypatch, dialect):
    from hashlib import sha256

    from aurora.database import history, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import create_table_sql, _quote
    from aurora.database.server_widening_history import widening_predecessor_models
    from aurora.database.server_migration_timeline import (ServerMigrationEntry,
                                                           verify_server_successor_current)

    operation = {"kind": "widen_column", "table": "expanded_names", "column": "name",
                 "from": "varchar(100)", "to": "varchar(200)"}
    predecessor = widening_predecessor_models([ServerHistoryExpandedName], operation)[0]
    original = sha256(create_table_sql(predecessor, dialect).encode()).hexdigest()
    table = _quote("expanded_names", dialect)
    field = _quote("name", dialect)
    alter_sql = (f"ALTER TABLE {table} ALTER COLUMN {field} TYPE VARCHAR(200)"
                 if dialect == "postgresql" else
                 f"ALTER TABLE {table} MODIFY COLUMN {field} VARCHAR(200) NOT NULL")
    alter = sha256(alter_sql.encode()).hexdigest()
    mapping = model_identity_map([ServerHistoryExpandedName])
    timeline = (ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                     "a" * 64, mapping, "b" * 64, "applied"),
                ServerMigrationEntry(2, "WidenName", "Untitled", "accounting",
                                     "c" * 64, mapping, "d" * 64, "applied",
                                     operation=operation))
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: timeline)
    entries = [HistoryEntry("initial", "expanded_names", dialect, original, "applied"),
               HistoryEntry("alter", "expanded_names", dialect, alter, "applied")]
    monkeypatch.setattr(history, "list_history", lambda _: tuple(entries))
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())

    def verify_schema(conn, models, **kwargs):
        assert kwargs["historical_type_override"] == ("expanded_names", "name", "varchar(100)")
        return "d" * 64, "b" * 64

    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema", verify_schema)
    connection = FakeServer(dialect)
    assert verify_server_successor_current(connection, [ServerHistoryExpandedName], "accounting")

    entries[-1] = HistoryEntry("alter", "expanded_names", dialect, "e" * 64, "applied")
    assert not verify_server_successor_current(connection, [ServerHistoryExpandedName], "accounting")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_applied_nullability_verifies_original_and_current_physical_history(monkeypatch,
                                                                             dialect):
    from hashlib import sha256

    from aurora.database import history, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import _quote, create_table_sql
    from aurora.database.server_nullability_history import nullability_predecessor_models
    from aurora.database.server_migration_timeline import (ServerMigrationEntry,
                                                           verify_server_successor_current)

    model = ServerHistoryRequiredLabel
    operation = {"kind": "change_nullability", "table": "required_labels",
                 "column": "label", "from": True, "to": False}
    previous = nullability_predecessor_models([model], operation)[0]
    original = sha256(create_table_sql(previous, dialect).encode()).hexdigest()
    table_name = _quote("required_labels", dialect)
    column_name = _quote("label", dialect)
    alter = (f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET NOT NULL"
             if dialect == "postgresql" else
             f"ALTER TABLE {table_name} MODIFY COLUMN {column_name} VARCHAR(80) NOT NULL")
    mapping = model_identity_map([model])
    timeline = (ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                     "a" * 64, mapping, "b" * 64, "applied"),
                ServerMigrationEntry(2, "RequireLabel", "Untitled", "accounting",
                                     "c" * 64, mapping, "d" * 64, "applied",
                                     operation=operation))
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: timeline)
    records = [HistoryEntry("initial", "required_labels", dialect, original, "applied"),
               HistoryEntry("alter", "required_labels", dialect,
                            sha256(alter.encode()).hexdigest(), "applied")]
    monkeypatch.setattr(history, "list_history", lambda _: tuple(records))
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())
    proof = []

    def verify_schema(connection, models, **kwargs):
        proof.append(kwargs["historical_nullability_override"])
        return "d" * 64, "b" * 64

    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema", verify_schema)
    assert verify_server_successor_current(FakeServer(dialect), [model], "accounting")
    assert proof == [("required_labels", "label", True)]

    records[-1] = HistoryEntry("alter", "required_labels", dialect, "f" * 64, "applied")
    assert not verify_server_successor_current(FakeServer(dialect), [model], "accounting")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_applied_default_requires_original_and_current_fingerprints(monkeypatch, dialect):
    from hashlib import sha256

    from aurora.database import history, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry
    from aurora.database.model_identity import model_identity_map
    from aurora.database.schema import _quote, create_table_sql
    from aurora.database.server_default_history import default_addition_predecessor_models
    from aurora.database.server_migration_timeline import (ServerMigrationEntry,
                                                           verify_server_successor_current)

    model = ServerHistoryDefaultedLabel
    operation = {"kind": "add_default", "table": "defaulted_labels", "column": "label"}
    old = default_addition_predecessor_models([model], operation)[0]
    original = sha256(create_table_sql(old, dialect).encode()).hexdigest()
    table_name = _quote("defaulted_labels", dialect)
    column_name = _quote("label", dialect)
    alter = f"ALTER TABLE {table_name} ALTER COLUMN {column_name} SET DEFAULT 'new'"
    mapping = model_identity_map([model])
    timeline = (ServerMigrationEntry(1, "Initial", "Initial migration", "accounting",
                                     "a" * 64, mapping, "b" * 64, "applied"),
                ServerMigrationEntry(2, "DefaultLabel", "Untitled", "accounting",
                                     "c" * 64, mapping, "d" * 64, "applied", operation=operation))
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: timeline)
    history_rows = [HistoryEntry("created", "defaulted_labels", dialect, original, "applied"),
                    HistoryEntry("alter", "defaulted_labels", dialect,
                                 sha256(alter.encode()).hexdigest(), "applied")]
    monkeypatch.setattr(history, "list_history", lambda _: tuple(history_rows))
    monkeypatch.setattr(history, "review_history", lambda *_, **__: ())
    proof = []

    def verify_schema(connection, models, **kwargs):
        proof.append(kwargs["historical_default_exclusion"])
        return "d" * 64, "b" * 64

    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema", verify_schema)
    assert verify_server_successor_current(FakeServer(dialect), [model], "accounting")
    assert proof == [("defaulted_labels", "label")]

    history_rows[-1] = HistoryEntry("alter", "defaulted_labels", dialect, "f" * 64, "applied")
    assert not verify_server_successor_current(FakeServer(dialect), [model], "accounting")
