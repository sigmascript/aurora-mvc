"""MySQL destructive-column contracts: informed approval, no automatic backup."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database import connections as _identity_module
from aurora.database import column_operations as remove
from aurora.database.schema_inspection import ColumnInfo, TableInfo


def _schemas():
    prior = [
        {
            "name": "mysql_fk_child",
            "create_sql_preview": "old",
            "columns": [
                {"name": "id", "primary_key": True, "unique": False},
                {
                    "name": "parent_id",
                    "primary_key": False,
                    "unique": False,
                    "references": None,
                    "server_default": None,
                },
            ],
        }
    ]
    declared = deepcopy(prior)
    declared[0]["columns"].pop()
    declared[0]["create_sql_preview"] = "new"
    return prior, declared


class Fake:
    dialect = "mysql"

    def __init__(self, populated=True):
        self.populated = populated
        self.statements = []
        self.row_count = 1

    def fetch_first(self, sql, params=None):
        if "IS NOT NULL LIMIT 1" in sql:
            return {"present": 1} if self.populated else None
        if "COUNT(*) AS total" in sql:
            return {"total": self.row_count}
        return None

    def execute_write(self, sql, params=None):
        self.statements.append((sql, params))


def _review_setup(monkeypatch, populated=True, *, indexed=False, referenced=False):
    old, new = _schemas()
    model = SimpleNamespace(__table_name__="mysql_fk_child")
    monkeypatch.setattr(remove, "validate_init_models", lambda models, **kw: [model])
    monkeypatch.setattr(
        remove,
        "read_history",
        lambda *a: ([{"id": 1}], {"models": old, "physical_schema": [{"table": "mysql_fk_child"}]}),
    )
    monkeypatch.setattr(remove, "schema_snapshot", lambda *a, **kw: {"tables": new})
    monkeypatch.setattr(
        remove,
        "inspect_table",
        lambda *a: TableInfo(
            "mysql_fk_child",
            True,
            (
                ColumnInfo("id", "bigint", False, True),
                ColumnInfo("parent_id", "bigint", True, False),
            ),
            (),
            ("PRIMARY", "other") if indexed else ("PRIMARY",),
            True,
            True,
            (),
            (),
        ),
    )
    monkeypatch.setattr(remove, "_foreign_key_dependency", lambda *a: referenced)
    monkeypatch.setattr(remove, "_verify_simple_constraints", lambda *a, **kw: None)
    return Fake(populated), [model]


def test_mysql_risk_review_identifies_real_data(monkeypatch):
    conn, models = _review_setup(monkeypatch, populated=True)
    operation, digest, count = remove.review_mysql_column_removal(
        conn, models, ".", "data_mysql", "identity"
    )
    assert operation == {
        "kind": "drop_column",
        "table": "mysql_fk_child",
        "column": "parent_id",
        "has_meaningful_data": True,
    }
    assert count == 1 and len(digest) == 64
    empty, models = _review_setup(monkeypatch, populated=False)
    assert not remove.review_mysql_column_removal(empty, models, ".", "data_mysql", "identity")[0][
        "has_meaningful_data"
    ]


@pytest.mark.parametrize("flag", ["indexed", "referenced"])
def test_mysql_refuses_dependencies(monkeypatch, flag):
    conn, models = _review_setup(monkeypatch, **{flag: True})
    with pytest.raises(ValueError, match="dependenc"):
        remove.review_mysql_column_removal(conn, models, ".", "data_mysql", "identity")


def test_mysql_refuses_other_backend(monkeypatch):
    conn, models = _review_setup(monkeypatch)
    conn.dialect = "postgresql"
    with pytest.raises(ValueError, match="MySQL"):
        remove.review_mysql_column_removal(conn, models, ".", "data_mysql", "identity")


def _execution_setup(monkeypatch, populated=True):
    from contextlib import contextmanager

    from aurora.database import cli_init

    conn = Fake(populated)
    model = SimpleNamespace(__table_name__="mysql_fk_child")
    operation = {
        "kind": "drop_column",
        "table": "mysql_fk_child",
        "column": "parent_id",
        "has_meaningful_data": populated,
    }
    monkeypatch.setattr(remove.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(
        remove, "review_mysql_column_removal", lambda *a: (dict(operation), "a" * 64, 2)
    )
    monkeypatch.setattr(cli_init, "_mysql_database_identity", lambda *a: "same")
    monkeypatch.setattr(_identity_module, "_mysql_database_identity", lambda *a: "same")

    @contextmanager
    def held(connection):
        yield

    monkeypatch.setattr(remove, "migration_lock", held)
    monkeypatch.setattr(remove, "verify_server_declared_schema", lambda *a, **k: None)
    monkeypatch.setattr(remove, "_write_snapshot", lambda *a: SimpleNamespace(name="success.json"))
    monkeypatch.setattr(remove, "history_table", lambda *a: "__migrations__")
    monkeypatch.setattr(
        remove,
        "read_history",
        lambda *a: (
            [
                None,
                None,
                next(params for sql, params in conn.statements if sql.startswith("INSERT INTO")),
            ],
            {},
        ),
    )
    config = SimpleNamespace(resolve=lambda *a: "mysql+pymysql://user@localhost/data_mysql")
    args = SimpleNamespace(
        config="aurora_databases.json",
        confirm=None,
        title="Remove parent_id",
        apply=False,
        approve=None,
        backup=None,
        name=None,
    )
    return conn, [model], args, config


def test_populated_removal_is_allowed_without_backup_and_records_warning(monkeypatch, capsys):
    conn, models, args, config = _execution_setup(monkeypatch)
    approvals = iter(["y", "DELETE data_mysql.mysql_fk_child.parent_id"])
    monkeypatch.setattr("builtins.input", lambda _: next(approvals))
    monkeypatch.setattr(remove, "project_is_production", lambda root: False)
    monkeypatch.setattr(remove.os, "name", "nt")
    assert (
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
        == 0
    )
    output = capsys.readouterr().out
    assert "WARNING" in output and "aurora db backup" in output
    assert "will not automatically create or restore" in output
    assert sum("DROP COLUMN" in sql for sql, _ in conn.statements) == 1
    record = next(params for sql, params in conn.statements if sql.startswith("INSERT INTO"))
    assert "Confirmed permanent data loss" in record["warnings"]
    assert "backup not created automatically" in record["warnings"]


def test_wrong_confirmation_has_no_ddl(monkeypatch):
    conn, models, args, config = _execution_setup(monkeypatch)
    approvals = iter(["y", "wrong"])
    monkeypatch.setattr("builtins.input", lambda _: next(approvals))
    assert (
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
        == 0
    )
    assert not conn.statements


def test_empty_column_needs_only_ordinary_confirmation(monkeypatch):
    conn, models, args, config = _execution_setup(monkeypatch, populated=False)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert (
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
        == 0
    )
    assert any("DROP COLUMN" in sql for sql, _ in conn.statements)
    record = next(params for sql, params in conn.statements if sql.startswith("INSERT INTO"))
    assert record["warnings"] is None


def test_mysql_post_ddl_failure_is_explicitly_uncertain(monkeypatch):
    conn, models, args, config = _execution_setup(monkeypatch, populated=False)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr(
        remove,
        "_write_snapshot",
        lambda *a: (_ for _ in ()).throw(RuntimeError("snapshot failure")),
    )
    with pytest.raises(RuntimeError, match="outcome uncertain after DDL"):
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
    assert any("DROP COLUMN" in sql for sql, _ in conn.statements)


def test_noninteractive_and_automated_removals_refused(monkeypatch):
    conn, models, args, config = _execution_setup(monkeypatch)
    args.approve = "sha"
    with pytest.raises(PermissionError, match="ordinary interactive"):
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
    args.approve = None
    args.confirm = "yes"
    with pytest.raises(PermissionError, match="independent interactive"):
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)


def test_lock_precedes_mysql_ddl_without_backup_catalog_work(monkeypatch):
    from contextlib import contextmanager

    conn, models, args, config = _execution_setup(monkeypatch)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: (
            "DELETE data_mysql.mysql_fk_child.parent_id" if prompt.startswith("Type ") else "y"
        ),
    )
    timeline = []

    @contextmanager
    def lock(connection):
        timeline.append("lock")
        yield

    monkeypatch.setattr(remove, "migration_lock", lock)
    remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
    assert timeline == ["lock"]
    assert any("DROP COLUMN" in sql for sql, _ in conn.statements)


def test_changed_review_under_lock_refuses_ddl(monkeypatch):
    conn, models, args, config = _execution_setup(monkeypatch)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt: (
            "DELETE data_mysql.mysql_fk_child.parent_id" if prompt.startswith("Type ") else "y"
        ),
    )

    def changed_review(*a):
        if changed_review.calls:
            return (
                {
                    "kind": "drop_column",
                    "table": "mysql_fk_child",
                    "column": "parent_id",
                    "has_meaningful_data": False,
                },
                "b" * 64,
                2,
            )
        changed_review.calls += 1
        return (
            {
                "kind": "drop_column",
                "table": "mysql_fk_child",
                "column": "parent_id",
                "has_meaningful_data": True,
            },
            "a" * 64,
            2,
        )

    changed_review.calls = 0
    monkeypatch.setattr(remove, "review_mysql_column_removal", changed_review)
    with pytest.raises(RuntimeError, match="proposal changed after approval"):
        remove.run_mysql_column_removal(args, config, conn, models, ".", "data_mysql", "same", None)
    assert not conn.statements
