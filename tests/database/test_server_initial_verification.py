"""Initial server postconditions reject unknown objects before success evidence."""

from types import SimpleNamespace

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database.server_initial_verification import (server_default_matches,
                                                         verify_server_declared_schema,
                                                         verify_server_initial_schema)


class InitialArticle(Model):
    __table__ = "initial_articles"
    id = column(int, primary_key=True)
    note = column(str, nullable=True)


class InitialComment(Model):
    __table__ = "initial_comments"
    id = column(int, primary_key=True)


class ArticleDefaults(Model):
    __table__ = "article_defaults"
    id = column(int, primary_key=True)
    title = column(str, default="editor's draft")
    published = column(bool, default=False)
    count = column(int, default=12)


class UniqueArticle(Model):
    __table__ = "unique_articles"
    id = column(int, primary_key=True)
    title = column(str, length=60, unique=True, default="draft")
    published = column(bool, default=True)


class CatalogConnection:
    def __init__(self, dialect, *, generated=False):
        self.dialect = dialect
        self.generated = generated

    def fetch_first(self, sql, params=None):
        if self.dialect == "mysql":
            return None

        return {"persistence": "p", "partitioned": False, "inherited": False,
                "rls": False, "triggers": 0, "parents": 0}

    def fetch_all(self, sql, params=None):
        if "TABLE_CONSTRAINTS" in sql:
            return [{"kind": "PRIMARY KEY"}, {"kind": "UNIQUE"}]

        if "pg_catalog.pg_constraint" in sql:
            return [{"kind": "p"}, {"kind": "u"}]

        if "information_schema.COLUMNS" in sql:
            return [{"name": "id", "extra": "auto_increment", "generation": ""},
                    {"name": "title", "extra": "DEFAULT_GENERATED" if self.generated else "",
                     "generation": ""},
                    {"name": "published", "extra": "", "generation": ""}]

        if "pg_catalog.pg_attribute" in sql:
            return [{"name": "id", "identity": "d", "generated": ""},
                    {"name": "title", "identity": "", "generated": ""},
                    {"name": "published", "identity": "", "generated": ""}]

        raise AssertionError("Unexpected catalog query")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_verified_unique_and_literal_default_table(monkeypatch, dialect):
    from aurora.database import server_initial_verification

    sql_types = (["BIGINT", "varchar(60)", "tinyint(1)"] if dialect == "mysql" else
                 ["bigint", "character varying(60)", "boolean"])
    title_default = "draft" if dialect == "mysql" else "'draft'::character varying"
    enabled_default = "1" if dialect == "mysql" else "true"
    table = TableInfo("unique_articles", True, (
        ColumnInfo("id", sql_types[0], False, True),
        ColumnInfo("title", sql_types[1], False, False, title_default),
        ColumnInfo("published", sql_types[2], False, False, enabled_default),
    ), unique_columns=("title",), indexes=("pk", "unique"), uniqueness_verified=True)
    monkeypatch.setattr(server_initial_verification, "inspect_table", lambda *_: table)

    assert server_initial_verification._verify_declared_table(
        CatalogConnection(dialect), UniqueArticle) == table

    if dialect == "mysql":
        with pytest.raises(RuntimeError, match="generated"):
            server_initial_verification._verify_declared_table(
                CatalogConnection(dialect, generated=True), UniqueArticle)


@pytest.mark.parametrize("dialect,defaults", [
    ("mysql", {"title": "editor's draft", "published": "0", "count": "12"}),
    ("postgresql", {"title": "'editor''s draft'::text", "published": "false",
                     "count": "12"}),
])
def test_bounded_server_default_equivalence(dialect, defaults):
    for name, observed in defaults.items():
        assert server_default_matches(ArticleDefaults.__columns__[name], observed, dialect)

    assert server_default_matches(InitialArticle.__columns__["note"], None, dialect)
    assert not server_default_matches(ArticleDefaults.__columns__["title"],
                                      "now()", dialect)
    assert not server_default_matches(ArticleDefaults.__columns__["count"],
                                      "1 + 11", dialect)
    assert not server_default_matches(ArticleDefaults.__columns__["published"],
                                      "current_setting('something')", dialect)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_initial_postconditions_are_read_only_and_bound_to_observed_schema(monkeypatch, dialect):
    from aurora.database import server_initial_verification

    rows = ([{"name": name, "kind": "BASE TABLE" if dialect == "mysql" else "r"}
             for name in ("initial_articles", "aurorasql_schema_history",
                          "aurorasql_migration_timeline")])
    reads = []

    def fetch_all(sql):
        reads.append(sql)
        return rows

    connection = SimpleNamespace(dialect=dialect, fetch_all=fetch_all)
    verified = []
    monkeypatch.setattr(server_initial_verification, "_verify_declared_table",
                        lambda conn, model: verified.append(model.__table_name__) or TableInfo(
        "initial_articles", True, (ColumnInfo("id", "BIGINT", False, True),
                                   ColumnInfo("note", "TEXT", True, False)),
        indexes=("PRIMARY",), uniqueness_verified=True))

    digest = verify_server_initial_schema(connection, [InitialArticle])
    assert len(digest) == 64
    assert verified == ["initial_articles"]
    assert len(reads) == 1
    assert reads[0].startswith("SELECT ")

    rows.append({"name": "unexpected", "kind": "BASE TABLE" if dialect == "mysql" else "r"})

    with pytest.raises(RuntimeError, match="unexpected"):
        verify_server_initial_schema(connection, [InitialArticle])


def test_initial_postconditions_reject_server_views(monkeypatch):
    from aurora.database import server_initial_verification

    objects = [{"name": name, "kind": "r"} for name in (
        "initial_articles", "aurorasql_schema_history", "aurorasql_migration_timeline")]
    objects[-1]["kind"] = "v"
    connection = SimpleNamespace(dialect="postgresql", fetch_all=lambda *_: objects)
    monkeypatch.setattr(server_initial_verification, "_verify_declared_table",
                        lambda *_: pytest.fail("Must reject before per-table inspection"))

    with pytest.raises(RuntimeError, match="unexpected"):
        verify_server_initial_schema(connection, [InitialArticle])


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_successor_schema_evidence_checks_every_table_even_for_initial_digest(monkeypatch, dialect):
    from aurora.database import server_initial_verification

    objects = [{"name": name, "kind": "BASE TABLE" if dialect == "mysql" else "r"}
               for name in ("initial_articles", "initial_comments", "aurorasql_schema_history",
                            "aurorasql_migration_timeline")]
    connection = SimpleNamespace(dialect=dialect, fetch_all=lambda *_: objects)
    checked = []

    def verify_table(conn, model):
        checked.append(model.__table_name__)
        return TableInfo(model.__table_name__, True,
                         (ColumnInfo("id", "BIGINT", False, True),),
                         indexes=("PRIMARY",), uniqueness_verified=True)

    monkeypatch.setattr(server_initial_verification, "_verify_declared_table", verify_table)
    baseline = objects.pop(1)
    initial = verify_server_initial_schema(connection, [InitialArticle])
    objects.insert(1, baseline)
    checked.clear()

    preserved = verify_server_declared_schema(
        connection, [InitialArticle, InitialComment], evidence_models=[InitialArticle])
    assert preserved == initial
    assert checked == ["initial_articles", "initial_comments"]

    full = verify_server_declared_schema(connection, [InitialArticle, InitialComment])
    assert full != initial

    objects.append({"name": "unexpected", "kind": objects[0]["kind"]})

    with pytest.raises(RuntimeError, match="unexpected"):
        verify_server_declared_schema(connection, [InitialArticle, InitialComment],
                                      evidence_models=[InitialArticle])


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_column_successor_observes_current_and_original_digest_together(monkeypatch, dialect):
    from aurora.database import server_initial_verification

    objects = [{"name": name, "kind": "BASE TABLE" if dialect == "mysql" else "r"}
               for name in ("initial_articles", "aurorasql_schema_history",
                            "aurorasql_migration_timeline")]
    connection = SimpleNamespace(dialect=dialect, fetch_all=lambda *_: objects)
    columns = [ColumnInfo("id", "BIGINT", False, True)]
    monkeypatch.setattr(server_initial_verification, "_verify_declared_table",
                        lambda conn, model: TableInfo(model.__table_name__, True,
                                                       tuple(columns), indexes=("PRIMARY",),
                                                       uniqueness_verified=True))

    baseline = verify_server_declared_schema(connection, [InitialArticle])
    columns.append(ColumnInfo("note", "TEXT", True, False))
    current, historical = verify_server_declared_schema(
        connection, [InitialArticle], historical_exclusion=("initial_articles", "note"))
    assert current != baseline
    assert historical == baseline


@pytest.mark.parametrize("old_catalog,new_catalog,old_normalized", [
    ("character varying(80)", "character varying(160)", "varchar(80)"),
    ("numeric(8,2)", "numeric(12,2)", "decimal(8,2)"),
])
def test_postgres_widening_reconstructs_original_catalog_digest(
        monkeypatch, old_catalog, new_catalog, old_normalized):
    from aurora.database import server_initial_verification

    class WidenedNote(Model):
        __table__ = "widened_notes"

        id = column(int, primary_key=True)
        label = column(str, length=160)

    objects = [{"name": name, "kind": "r"} for name in
               ("widened_notes", "aurorasql_schema_history", "aurorasql_migration_timeline")]
    connection = SimpleNamespace(dialect="postgresql", fetch_all=lambda *_: objects)
    active_catalog = [old_catalog]

    def observed_table(_, model):
        return TableInfo(model.__table_name__, True, (
            ColumnInfo("id", "bigint", False, True),
            ColumnInfo("label", active_catalog[0], False, False),
        ), indexes=("widened_notes_pkey",), uniqueness_verified=True)

    monkeypatch.setattr(server_initial_verification, "_verify_declared_table", observed_table)
    initial_digest = verify_server_declared_schema(connection, [WidenedNote])
    active_catalog[0] = new_catalog
    current, reconstructed = verify_server_declared_schema(
        connection, [WidenedNote],
        historical_type_override=("widened_notes", "label", old_normalized))

    assert reconstructed == initial_digest
    assert current != initial_digest
