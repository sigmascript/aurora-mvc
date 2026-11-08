"""Server model review never authorizes a write or tolerates mixed drift."""

from types import SimpleNamespace
from contextlib import contextmanager

import pytest

from aurora.database import Model, column
from aurora.database.inspection import ColumnInfo, TableInfo
from aurora.database.planning import SchemaChange, SchemaPlan
from aurora.database.server_model_review import review_server_model_changes


class ServerArticle(Model):
    __table__ = "server_articles"
    id = column(int, primary_key=True)


class ServerArticleWithNote(Model):
    __table__ = "server_articles"
    id = column(int, primary_key=True)
    note = column(str, nullable=True, default="draft")


class ServerArticleWithRequiredNote(Model):
    __table__ = "server_articles"
    id = column(int, primary_key=True)
    note = column(str)


class ServerComment(Model):
    __table__ = "server_comments"
    id = column(int, primary_key=True)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_review_fingerprints_missing_table_without_execution(monkeypatch, dialect):
    from aurora.database import server_model_review

    monkeypatch.setattr(server_model_review, "plan_schema", lambda *_: SchemaPlan(
        "server_articles", False, (SchemaChange("create_table", None, "missing"),), ()))
    connection = SimpleNamespace(dialect=dialect)
    review = review_server_model_changes(connection, [ServerArticle], "default", "test")

    assert review.operations == ({"kind": "create_table", "table": "server_articles"},)
    assert review.statements[0].startswith("CREATE TABLE")
    assert not review.execution_allowed
    assert len(review.review_sha256) == 64
    assert review.review_sha256 != review_server_model_changes(
        connection, [ServerArticle], "accounting", "test"
    ).review_sha256


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_review_nullable_constant_default_and_required_refusal(monkeypatch, dialect):
    from aurora.database import server_model_review

    monkeypatch.setattr(server_model_review, "plan_schema", lambda *_: SchemaPlan(
        "server_articles", True, (SchemaChange("missing_column", "note", "missing"),), ()))
    monkeypatch.setattr(server_model_review, "inspect_table", lambda *_: TableInfo(
        "server_articles", True, (ColumnInfo("id", "BIGINT", False, True),),
        indexes=("PRIMARY",) if dialect == "mysql" else ("server_articles_pkey",),
        uniqueness_verified=True))
    connection = SimpleNamespace(dialect=dialect)
    review = review_server_model_changes(
        connection, [ServerArticleWithNote], "default", "test")

    assert "ADD COLUMN" in review.statements[0]
    assert "DEFAULT 'draft'" in review.statements[0]
    assert not review.execution_allowed

    with pytest.raises(ValueError, match="constraint or backfill"):
        review_server_model_changes(
            connection, [ServerArticleWithRequiredNote], "default", "test")


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_server_review_refuses_existing_column_drift(monkeypatch, dialect):
    from aurora.database import server_model_review

    monkeypatch.setattr(server_model_review, "plan_schema", lambda *_: SchemaPlan(
        "server_articles", True, (SchemaChange("type_mismatch", "id", "changed"),), ()))

    with pytest.raises(ValueError, match="existing-column drift"):
        review_server_model_changes(SimpleNamespace(dialect=dialect), [ServerArticle],
                                    "default", "test")


def test_server_review_refuses_unidentified_database_and_multiple_operations(monkeypatch):
    from aurora.database import server_model_review

    monkeypatch.setattr(server_model_review, "plan_schema", lambda *_: SchemaPlan(
        "server_articles", False, (SchemaChange("create_table", None, "missing"),), ()))
    connection = SimpleNamespace(dialect="mysql")

    with pytest.raises(ValueError, match="identity"):
        review_server_model_changes(connection, [ServerArticle], "default", "")

    with pytest.raises(ValueError, match="exactly one"):
        review_server_model_changes(connection, [ServerArticle, ServerComment],
                                    "default", "test")


def test_server_review_refuses_secondary_index_before_column_addition(monkeypatch):
    from aurora.database import server_model_review

    monkeypatch.setattr(server_model_review, "plan_schema", lambda *_: SchemaPlan(
        "server_articles", True, (SchemaChange("missing_column", "note", "missing"),), ()))
    monkeypatch.setattr(server_model_review, "inspect_table", lambda *_: TableInfo(
        "server_articles", True, (ColumnInfo("id", "BIGINT", False, True),),
        indexes=("PRIMARY", "custom_index"), uniqueness_verified=True))

    with pytest.raises(ValueError, match="Indexed source"):
        review_server_model_changes(SimpleNamespace(dialect="mysql"),
                                    [ServerArticleWithNote], "default", "test")


def test_legacy_server_dry_run_delegates_to_plan(monkeypatch):
    from aurora.database import cli_model_migration, cli_plan

    config = SimpleNamespace(resolve=lambda _: "mysql+pymysql://localhost/test")
    monkeypatch.setattr(cli_model_migration, "_load_config", lambda _: (config, "/project"))
    monkeypatch.setattr(cli_model_migration, "_select_models", lambda *_: (
        ["models:ServerArticle"], [ServerArticle]))
    calls = []
    monkeypatch.setattr(cli_plan, "run_plan", lambda args: calls.append(args) or 0)
    args = SimpleNamespace(
        review_file=None, changes=None, name=None, schema=None, table=None,
        column=None, approve_review=None, approve_sql=None, output_dir="migrations",
        dry_run=True, apply=False, approve=None, confirm=None, backup=None,
        title=None, config="unused", database="default",
    )

    assert cli_model_migration.run_model_migration(args) == 0
    assert calls == [args]


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_plan_owns_server_review_and_refuses_unresolved_history(monkeypatch, capsys, dialect):
    import json
    from aurora.database import cli_init, cli_plan, history, server_migration_timeline
    from aurora.database.server_model_review import ServerModelProposal

    @contextmanager
    def connect(alias):
        assert alias == "default"
        yield SimpleNamespace(dialect=dialect)

    config = SimpleNamespace(connect=connect)
    monkeypatch.setattr(cli_plan, "_load_config", lambda _: (config, "/project"))
    monkeypatch.setattr(cli_plan, "_select_models", lambda *_: (["app:ServerArticle"], [ServerArticle]))
    monkeypatch.setattr(cli_plan, "plan_schema", lambda *_: SchemaPlan(
        "server_articles", False, (SchemaChange("create_table", None, "missing"),), ()))
    monkeypatch.setattr(history, "review_history", lambda _: ())
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *_: "mysql-test")
    monkeypatch.setattr(cli_init, "_postgres_database_identity", lambda *_: "postgres-test")
    monkeypatch.setattr(server_migration_timeline, "inspect_server_timeline", lambda *_: None)
    from aurora.database import server_model_review
    monkeypatch.setattr(server_model_review, "review_server_model_changes", lambda *args: ServerModelProposal(
        dialect, ("CREATE TABLE x",), ({"kind": "create_table", "table": "x"},), "a" * 64))
    args = SimpleNamespace(database="default", config="unused", models_config="aurora_models.json",
                           format="json", check=False)
    monkeypatch.setattr(config, "resolve", lambda _: f"{dialect}+{'pymysql' if dialect == 'mysql' else 'psycopg'}://localhost/test",
                        raising=False)

    assert cli_plan.run_plan(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["server_migration_review"]["execution_allowed"] is False
    assert report["server_migration_review"]["operations"][0]["kind"] == "create_table"

    monkeypatch.setattr(history, "review_history", lambda _: ("unresolved",))
    assert cli_plan.run_plan(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["server_migration_review"] is None
    assert "unresolved" in report["server_migration_warning"]
