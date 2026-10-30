"""Broad SQLite workflows for read-only inspection and conservative planning."""

from aurora.aurorasql import Model, column, connect, create_table, inspect_table, plan_schema


class Note(Model):
    id = column(int, primary_key=True)
    title = column(str, length=80)
    active = column(bool, default=False)


def test_inspect_and_plan_existing_sqlite_table():
    with connect("sqlite:///:memory:") as db:
        missing = inspect_table(db, "note")
        assert not missing.exists
        preview = plan_schema(db, Note)
        assert preview.changes[0].kind == "create_table"
        assert preview.create_sql_preview.startswith("CREATE TABLE")
        # Planning did not create the table.
        assert not inspect_table(db, "note").exists
        create_table(db, Note)
        observed = inspect_table(db, "note")
        assert observed.exists and len(observed.columns) == 3
        plan = plan_schema(db, Note)
        assert not plan.changes
        assert plan.requires_review  # Unverified constraints remain explicit.
        assert plan.create_sql_preview is None


def test_plan_differences_without_changing_existing_table():
    with connect("sqlite:///:memory:") as db:
        db.execute_write('CREATE TABLE "note" ("id" INTEGER PRIMARY KEY, "title" TEXT, "legacy" TEXT)')
        plan = plan_schema(db, Note)
        kinds = {change.kind for change in plan.changes}
        assert {"missing_column", "extra_column", "nullability_mismatch"} <= kinds
        assert len(inspect_table(db, "note").columns) == 3


def test_schema_inspection_rejects_unsafe_identifier():
    with connect("sqlite:///:memory:") as db:
        import pytest
        with pytest.raises(ValueError):
            inspect_table(db, 'note"; DROP TABLE note; --')


def test_unique_index_and_default_are_inspected_without_mutation():
    class Account(Model):
        id = column(int, primary_key=True)
        email = column(str, length=100, unique=True)
        nickname = column(str, nullable=True)

    with connect("sqlite:///:memory:") as db:
        db.execute_write(
            'CREATE TABLE "account" ('
            '"id" INTEGER PRIMARY KEY, '
            '"email" VARCHAR(100) NOT NULL UNIQUE, '
            '"nickname" TEXT DEFAULT \'guest\')'
        )
        observed = inspect_table(db, "account")
        assert "email" in observed.unique_columns
        assert observed.constraints_verified
        assert observed.indexes
        assert next(c for c in observed.columns if c.name == "nickname").default_sql is not None

        plan = plan_schema(db, Account)
        assert not any(change.kind == "unique_mismatch" for change in plan.changes)
        assert not plan.execution_allowed
        assert any("default" in warning.lower() for warning in plan.warnings)

        db.execute_write('CREATE UNIQUE INDEX "nickname_unique" ON "account" ("nickname")')
        mismatch = plan_schema(db, Account)
        assert any(c.kind == "unique_mismatch" and c.column == "nickname"
                   for c in mismatch.changes)
        assert not mismatch.execution_allowed


def test_composite_unique_index_is_not_misreported_as_column_unique():
    class Pair(Model):
        id = column(int, primary_key=True)
        left = column(str)
        right = column(str)

    with connect("sqlite:///:memory:") as db:
        db.execute_write(
            'CREATE TABLE "pair" ("id" INTEGER PRIMARY KEY, '
            '"left" TEXT NOT NULL, "right" TEXT NOT NULL, '
            'UNIQUE ("left", "right"))'
        )
        observed = inspect_table(db, "pair")
        assert "left" not in observed.unique_columns
        assert "right" not in observed.unique_columns
        assert not any(c.kind == "unique_mismatch" for c in plan_schema(db, Pair).changes)
