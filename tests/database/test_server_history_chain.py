"""Pure history reconstruction checks for repeated guarded server migrations."""

from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from aurora.database import Model, column
from aurora.database.server_history_chain import (
    catalog_fingerprint,
    expected_server_journal,
    preceding_observations,
    predecessor_models,
    reviewed_statement,
    verify_server_applied_chain,
    verify_server_review_predecessor,
)
from aurora.database.server_migration_timeline import ServerMigrationEntry


class ChangingInvoice(Model):
    __table__ = "invoices"

    id = column(int, primary_key=True)
    amount = column("DECIMAL(16,2)")


class AccountLedger(Model):
    __table__ = "accounts"

    id = column(int, primary_key=True)


class AddedLedger(Model):
    __table__ = "ledgers"

    id = column(int, primary_key=True)
    label = column(str, length=80, nullable=True)


class DefaultedCmsPost(Model):
    __table__ = "cms_posts"

    id = column(int, primary_key=True)
    status = column(str, length=40, nullable=True, default="draft")


class RelatedCmsComment(Model):
    __table__ = "cms_comments"

    id = column(int, primary_key=True)
    post_id = column(int, references="cms_posts.id")


class ExpandedCmsPost(Model):
    __table__ = "cms_posts"

    id = column(int, primary_key=True)
    slug = column(str, length=160, nullable=True)
    status = column(str, length=40, nullable=True, default="draft")


def _observations(amount_type):
    return [{"table": "invoices",
             "columns": [("id", "bigint", False, True, None),
                         ("amount", amount_type, False, False, None)],
             "indexes": ["PRIMARY"], "unique_columns": []}]


def _review_hash(dialect, operation, statement):
    payload = json.dumps({"dialect": dialect, "alias": "default", "identity": "test:invoice",
                          "operations": (operation,), "statements": (statement,)},
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def test_reverse_widenings_preserves_application_model():
    most_recent = {"kind": "widen_column", "table": "invoices", "column": "amount",
                   "from": "decimal(12,2)", "to": "decimal(16,2)"}
    earlier = {"kind": "widen_column", "table": "invoices", "column": "amount",
               "from": "decimal(10,2)", "to": "decimal(12,2)"}

    intermediate = predecessor_models([ChangingInvoice], most_recent)
    initial = predecessor_models(intermediate, earlier)

    assert intermediate[0].__columns__["amount"].sql_type == "DECIMAL(12,2)"
    assert initial[0].__columns__["amount"].sql_type == "DECIMAL(10,2)"
    assert ChangingInvoice.__columns__["amount"].sql_type == "DECIMAL(16,2)"


@pytest.mark.parametrize("dialect,raw_type", [
    ("mysql", "decimal(16,2)"),
    ("postgresql", "numeric(16,2)"),
])
def test_four_entry_accounting_widening_chain_reconstructs_every_predecessor(
        dialect, raw_type):
    operations = [
        {"kind": "widen_column", "table": "invoices", "column": "amount",
         "from": "decimal(10,2)", "to": "decimal(12,2)"},
        {"kind": "widen_column", "table": "invoices", "column": "amount",
         "from": "decimal(12,2)", "to": "decimal(14,2)"},
        {"kind": "widen_column", "table": "invoices", "column": "amount",
         "from": "decimal(14,2)", "to": "decimal(16,2)"},
    ]
    states = [None] * 4
    catalog = [None] * 4
    states[3] = (ChangingInvoice,)
    catalog[3] = _observations(raw_type)

    for number in range(3, 0, -1):
        states[number - 1] = predecessor_models(states[number], operations[number - 1], dialect)
        catalog[number - 1] = preceding_observations(
            catalog[number], operations[number - 1], dialect)

    mapping = {f"{ChangingInvoice.__module__}:{ChangingInvoice.__qualname__}": "invoices"}
    history = [ServerMigrationEntry(
        1, "Initial", "Initial migration", "default", "a" * 64, mapping,
        catalog_fingerprint(dialect, catalog[0]), "applied")]

    for number, operation in enumerate(operations, 2):
        statement = reviewed_statement(states[number - 1], operation, dialect)
        history.append(ServerMigrationEntry(
            number, f"Migration{number}", "Accounting precision", "default",
            _review_hash(dialect, operation, statement), mapping,
            catalog_fingerprint(dialect, catalog[number - 1]), "applied",
            operation=operation))

    assert sum(expected_server_journal(states[-1], history, dialect,
                                       "test:invoice", catalog[-1]).values()) == 4
    assert states[0][0].__columns__["amount"].sql_type == "DECIMAL(10,2)"


@pytest.mark.parametrize("dialect,raw_type", [
    ("mysql", "decimal(16,2)"),
    ("postgresql", "numeric(16,2)"),
])
def test_history_reconstruction_verifies_three_entry_chain(monkeypatch, dialect, raw_type):
    newest = {"kind": "widen_column", "table": "invoices", "column": "amount",
              "from": "decimal(12,2)", "to": "decimal(16,2)"}
    earlier = {"kind": "widen_column", "table": "invoices", "column": "amount",
               "from": "decimal(10,2)", "to": "decimal(12,2)"}
    intermediate = predecessor_models([ChangingInvoice], newest)
    newest_sql = reviewed_statement([ChangingInvoice], newest, dialect)
    earlier_sql = reviewed_statement(intermediate, earlier, dialect)
    identity = {f"{ChangingInvoice.__module__}:{ChangingInvoice.__qualname__}": "invoices"}
    recent_catalog = _observations(raw_type)
    middle_catalog = preceding_observations(recent_catalog, newest, dialect)
    initial_catalog = preceding_observations(middle_catalog, earlier, dialect)
    history = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             identity, catalog_fingerprint(dialect, initial_catalog), "applied"),
        ServerMigrationEntry(2, "Migration2", "First widening", "default",
                             _review_hash(dialect, earlier, earlier_sql), identity,
                             catalog_fingerprint(dialect, middle_catalog), "applied",
                             operation=earlier),
        ServerMigrationEntry(3, "Migration3", "Second widening", "default",
                             _review_hash(dialect, newest, newest_sql), identity,
                             catalog_fingerprint(dialect, recent_catalog), "applied",
                             operation=newest),
    )

    fingerprints = expected_server_journal([ChangingInvoice], history, dialect,
                                           "test:invoice", recent_catalog)

    assert sum(fingerprints.values()) == 3

    from aurora.database import history as history_module
    from aurora.database import inspection, server_initial_verification, server_migration_timeline
    from aurora.database.history import HistoryEntry

    fake_connection = SimpleNamespace(dialect=dialect, fetch_all=lambda *_: [])
    journal = tuple(HistoryEntry(str(number), table, dialect, fingerprint, "applied")
                    for number, (table, fingerprint) in enumerate(fingerprints.elements()))
    columns = [SimpleNamespace(name=name, primary_key=(name == "operation_id"),
                               nullable=False) for name in
               ("operation_id", "table_name", "dialect", "fingerprint", "status")]
    monkeypatch.setattr(inspection, "inspect_table", lambda *_: SimpleNamespace(
        exists=True, columns=columns, uniqueness_verified=True,
        unique_columns=(), indexes=("PRIMARY",)))
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *_: history)
    monkeypatch.setattr(server_initial_verification, "verify_server_declared_schema",
                        lambda *_ , **__: (history[-1].schema_sha256, recent_catalog))
    monkeypatch.setattr(history_module, "list_history", lambda *_: journal)

    assert verify_server_applied_chain(fake_connection, [ChangingInvoice],
                                       "default", "test:invoice")

    fake_connection.fetch_all = lambda *_: [{"name": "unexpected_trigger"}]
    assert not verify_server_applied_chain(fake_connection, [ChangingInvoice],
                                           "default", "test:invoice")

    fake_connection.fetch_all = lambda *_: []
    monkeypatch.setattr(inspection, "inspect_table", lambda *_: SimpleNamespace(
        exists=True, columns=columns, uniqueness_verified=True,
        unique_columns=(), indexes=("PRIMARY", "unexpected_index")))
    assert not verify_server_applied_chain(fake_connection, [ChangingInvoice],
                                           "default", "test:invoice")

    monkeypatch.setattr(inspection, "inspect_table", lambda *_: SimpleNamespace(
        exists=True, columns=columns, uniqueness_verified=True,
        unique_columns=(), indexes=("PRIMARY",)))
    corrupted_journal = list(journal)
    original_entry = corrupted_journal[0]
    corrupted_journal[0] = HistoryEntry(original_entry.operation_id, original_entry.table,
                                        dialect, "0" * 64, "applied")
    monkeypatch.setattr(history_module, "list_history", lambda *_: corrupted_journal)
    assert not verify_server_applied_chain(fake_connection, [ChangingInvoice],
                                           "default", "test:invoice")

    monkeypatch.setattr(history_module, "list_history", lambda *_: journal + journal[:1])
    assert not verify_server_applied_chain(fake_connection, [ChangingInvoice],
                                           "default", "test:invoice")

    corrupted = history[:2] + (ServerMigrationEntry(
        3, "Migration3", "Second widening", "default", "0" * 64,
        identity, history[2].schema_sha256, "applied", operation=newest),)

    with pytest.raises(RuntimeError, match="reviewed server statement"):
        expected_server_journal([ChangingInvoice], corrupted, dialect,
                                "test:invoice", recent_catalog)


def test_history_catalog_rejects_unproven_type_transition():
    invalid = {"kind": "widen_column", "table": "invoices", "column": "amount",
               "from": "decimal(18,2)", "to": "decimal(16,2)"}

    with pytest.raises(ValueError, match="incompatible"):
        preceding_observations(_observations("decimal(16,2)"), invalid, "mysql")


def test_repeated_chain_refuses_relationship_constraints_without_proof():
    operation = {"kind": "add_column", "table": "cms_comments", "column": "post_id"}

    with pytest.raises(ValueError, match="Dependent historical column"):
        predecessor_models([RelatedCmsComment], operation)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_repeated_chain_preserves_prior_nullable_sql_default_addition(dialect):
    later = {"kind": "widen_column", "table": "cms_posts", "column": "slug",
             "from": "varchar(80)", "to": "varchar(160)"}
    earlier = {"kind": "add_column", "table": "cms_posts", "column": "status"}
    models = [ExpandedCmsPost]
    intermediate = predecessor_models(models, later, dialect)
    initial = predecessor_models(intermediate, earlier, dialect)
    raw_type = "character varying(160)" if dialect == "postgresql" else "varchar(160)"
    observations = [{"table": "cms_posts",
                     "columns": [("id", "bigint", False, True, None),
                                 ("slug", raw_type, True, False, None),
                                 ("status", "character varying(40)" if dialect == "postgresql"
                                  else "varchar(40)", True, False,
                                  "'draft'::character varying" if dialect == "postgresql"
                                  else "draft")],
                     "indexes": ["cms_posts_pkey" if dialect == "postgresql" else "PRIMARY"],
                     "unique_columns": []}]
    middle = preceding_observations(observations, later, dialect)
    beginning = preceding_observations(middle, earlier, dialect)
    identity = {f"{ExpandedCmsPost.__module__}:{ExpandedCmsPost.__qualname__}": "cms_posts"}
    history = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             identity, catalog_fingerprint(dialect, beginning), "applied"),
        ServerMigrationEntry(2, "Migration2", "New status", "default",
                             _review_hash(dialect, earlier, reviewed_statement(intermediate,
                                                                               earlier, dialect)),
                             identity, catalog_fingerprint(dialect, middle), "applied",
                             operation=earlier),
        ServerMigrationEntry(3, "Migration3", "Longer slug", "default",
                             _review_hash(dialect, later, reviewed_statement(models, later, dialect)),
                             identity, catalog_fingerprint(dialect, observations), "applied",
                             operation=later),
    )

    assert "status" not in initial[0].__columns__
    assert sum(expected_server_journal(models, history, dialect,
                                       "test:invoice", observations).values()) == 3


def test_repeated_chain_can_create_table_then_add_plain_column():
    dialect = "mysql"
    created = {"kind": "create_table", "table": "ledgers"}
    added = {"kind": "add_column", "table": "ledgers", "column": "label"}
    latest_models = [AccountLedger, AddedLedger]
    before_addition = predecessor_models(latest_models, added)
    created_sql = reviewed_statement(before_addition, created, dialect)
    addition_sql = reviewed_statement(latest_models, added, dialect)
    mapping = {f"{model.__module__}:{model.__qualname__}": model.__table_name__
               for model in latest_models}
    original_mapping = {f"{AccountLedger.__module__}:{AccountLedger.__qualname__}": "accounts"}
    present = [
        {"table": "accounts", "columns": [("id", "bigint", False, True, None)],
         "indexes": ["PRIMARY"], "unique_columns": []},
        {"table": "ledgers", "columns": [("id", "bigint", False, True, None),
                                           ("label", "varchar(80)", True, False, None)],
         "indexes": ["PRIMARY"], "unique_columns": []},
    ]
    prior = preceding_observations(present, added, dialect)
    initial = preceding_observations(prior, created, dialect)
    history = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             original_mapping, catalog_fingerprint(dialect, initial), "applied"),
        ServerMigrationEntry(2, "Migration2", "New table", "default",
                             _review_hash(dialect, created, created_sql), mapping,
                             catalog_fingerprint(dialect, prior), "applied", operation=created),
        ServerMigrationEntry(3, "Migration3", "New column", "default",
                             _review_hash(dialect, added, addition_sql), mapping,
                             catalog_fingerprint(dialect, present), "applied", operation=added),
    )

    assert sum(expected_server_journal(latest_models, history, dialect,
                                       "test:invoice", present).values()) == 3


def test_history_rejects_modified_model_mapping():
    op = {"kind": "widen_column", "table": "invoices", "column": "amount",
          "from": "decimal(12,2)", "to": "decimal(16,2)"}
    statement = reviewed_statement([ChangingInvoice], op, "mysql")
    present = _observations("decimal(16,2)")
    previous = preceding_observations(present, op, "mysql")
    wrong_mapping = {"models.Unknown:Unknown": "invoices"}
    history = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             wrong_mapping, catalog_fingerprint("mysql", previous), "applied"),
        ServerMigrationEntry(2, "Migration2", "Widen", "default",
                             _review_hash("mysql", op, statement), wrong_mapping,
                             catalog_fingerprint("mysql", present), "applied", operation=op),
    )

    with pytest.raises(RuntimeError, match="model mapping"):
        expected_server_journal([ChangingInvoice], history, "mysql", "test:invoice", present)


def test_next_review_uses_verified_previous_model_state(monkeypatch):
    from aurora.database import server_history_chain, server_migration_timeline
    from aurora.database.model_identity import model_identity_map

    mapping = model_identity_map([ChangingInvoice])
    timeline = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             mapping, "b" * 64, "applied"),
        ServerMigrationEntry(2, "Migration2", "Prior edit", "default", "c" * 64,
                             mapping, "d" * 64, "applied"),
    )
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *_: timeline)
    reviewed = SimpleNamespace(operations=({"kind": "widen_column", "table": "invoices",
                                            "column": "amount", "from": "decimal(12,2)",
                                            "to": "decimal(16,2)"},))
    verified = []

    def inspect_predecessor(connection, models, alias, identity, *, current_model_mapping):
        verified.append((models[0].__columns__["amount"].sql_type, current_model_mapping))
        return True

    monkeypatch.setattr(server_history_chain, "verify_server_applied_chain",
                        inspect_predecessor)
    connection = SimpleNamespace(dialect="mysql")

    assert verify_server_review_predecessor(connection, [ChangingInvoice], "default",
                                            "test:invoice", reviewed) == 3
    assert verified == [("DECIMAL(12,2)", mapping)]


def test_next_review_refuses_unverified_previous_chain(monkeypatch):
    from aurora.database import server_history_chain, server_migration_timeline
    from aurora.database.model_identity import model_identity_map

    mapping = model_identity_map([ChangingInvoice])
    timeline = (
        ServerMigrationEntry(1, "Initial", "Initial migration", "default", "a" * 64,
                             mapping, "b" * 64, "applied"),
        ServerMigrationEntry(2, "Migration2", "Prior edit", "default", "c" * 64,
                             mapping, "d" * 64, "applied"),
    )
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline",
                        lambda *_: timeline)
    monkeypatch.setattr(server_history_chain, "verify_server_applied_chain",
                        lambda *_ , **__: False)
    reviewed = SimpleNamespace(operations=({"kind": "widen_column", "table": "invoices",
                                            "column": "amount", "from": "decimal(12,2)",
                                            "to": "decimal(16,2)"},))

    with pytest.raises(RuntimeError, match="unverified"):
        verify_server_review_predecessor(SimpleNamespace(dialect="mysql"),
                                         [ChangingInvoice], "default", "test:invoice", reviewed)


def test_reconstructs_supported_sql_default_without_mutating_cms_model():
    operation = {"kind": "add_default", "table": "cms_posts", "column": "status"}
    old_model = predecessor_models([DefaultedCmsPost], operation)[0]
    observations = [{"table": "cms_posts",
                     "columns": [("id", "bigint", False, True, None),
                                 ("status", "varchar(40)", True, False, "draft")],
                     "indexes": ["PRIMARY"], "unique_columns": []}]

    assert old_model.__columns__["status"].default is None
    assert DefaultedCmsPost.__columns__["status"].default == "draft"
    assert preceding_observations(observations, operation, "mysql")[0]["columns"][1][-1] is None


def test_reconstructs_nullable_to_required_predecessor():
    class RequiredInvoice(Model):
        __table__ = "required_invoices"

        id = column(int, primary_key=True)
        description = column(str, length=80)

    operation = {"kind": "change_nullability", "table": "required_invoices",
                 "column": "description", "from": True, "to": False}
    previous = predecessor_models([RequiredInvoice], operation)[0]
    observations = [{"table": "required_invoices",
                     "columns": [("id", "bigint", False, True, None),
                                 ("description", "varchar(80)", False, False, None)],
                     "indexes": ["PRIMARY"], "unique_columns": []}]

    assert previous.__columns__["description"].nullable is True
    assert RequiredInvoice.__columns__["description"].nullable is False
    assert preceding_observations(observations, operation, "mysql")[0]["columns"][1][2] is True
