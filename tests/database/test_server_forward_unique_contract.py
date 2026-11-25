"""Single-column server UNIQUE review: data, identity and fail-closed boundaries."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from aurora.database import constraints as unique
from aurora.database import foreign_keys as foreign_key_checks
from aurora.database.schema_inspection import ColumnInfo, TableInfo


class Catalog:
    dialect = "mysql"

    def __init__(self, *, duplicates=False):
        self.duplicates = duplicates
        self.queries = []

    def fetch_first(self, sql, params=None):
        self.queries.append(sql)
        if "HAVING COUNT(*) > 1" in sql:
            return {"present": 1} if self.duplicates else None
        return None


def _models_and_snapshot(*, enable=True, other_change=False):
    old = {
        "name": "mysql_item",
        "create_sql_preview": "old ddl",
        "columns": [
            {"name": "id", "type": "int", "nullable": False, "primary_key": True, "unique": False},
            {
                "name": "category",
                "type": "str",
                "nullable": True,
                "primary_key": False,
                "unique": not enable,
            },
        ],
    }
    new = deepcopy(old)
    new["create_sql_preview"] = "new ddl"
    new["columns"][1]["unique"] = enable
    if other_change:
        new["columns"][1]["nullable"] = False
    predecessor = {"models": [old], "physical_schema": [{"table": "mysql_item"}]}
    return new, predecessor


def _review_setup(
    monkeypatch,
    *,
    enable=True,
    duplicates=False,
    other_change=False,
    fk=False,
    indexes=None,
    installed=None,
):
    new, predecessor = _models_and_snapshot(enable=enable, other_change=other_change)
    dummy = SimpleNamespace(__table_name__="mysql_item")
    monkeypatch.setattr(unique, "validate_init_models", lambda models, **kw: (dummy,))
    monkeypatch.setattr(unique, "read_history", lambda *args: ([{"id": 1}], predecessor))
    monkeypatch.setattr(unique, "schema_snapshot", lambda *args, **kw: {"tables": [new]})
    monkeypatch.setattr(
        unique,
        "inspect_table",
        lambda *args: TableInfo(
            "mysql_item",
            True,
            (
                ColumnInfo("id", "bigint", False, True),
                ColumnInfo("category", "varchar(60)", True, False),
            ),
            ("category",) if not enable else (),
            indexes
            or (("PRIMARY", "aurora_uq_mysql_item_category") if not enable else ("PRIMARY",)),
            True,
            True,
        ),
    )
    monkeypatch.setattr(foreign_key_checks, "_foreign_key_dependency", lambda *args: fk)
    monkeypatch.setattr(unique, "_verify_simple_constraints", lambda *args, **kwargs: None)
    monkeypatch.setattr(unique, "_constraint_columns", lambda *args: installed)
    return Catalog(duplicates=duplicates), [dummy]


def test_add_unique_requires_real_duplicate_check(monkeypatch):
    connection, models = _review_setup(monkeypatch)
    operation, digest, count = unique.review_unique_change(
        connection, models, ".", "data_mysql", "identity"
    )
    assert operation["kind"] == "add_unique"
    assert operation["name"] == "aurora_uq_mysql_item_category"
    assert "ADD CONSTRAINT" in operation["sql"]
    assert len(digest) == 64 and count == 1
    assert any("HAVING COUNT(*) > 1" in sql for sql in connection.queries)


def test_add_unique_refuses_duplicate_rows(monkeypatch):
    connection, models = _review_setup(monkeypatch, duplicates=True)
    with pytest.raises(ValueError, match="Existing duplicate values prevent UNIQUE"):
        unique.review_unique_change(connection, models, ".", "data_mysql", "identity")


def test_unique_refuses_unrelated_model_edits(monkeypatch):
    connection, models = _review_setup(monkeypatch, other_change=True)
    with pytest.raises(ValueError, match="unrelated column changes"):
        unique.review_unique_change(connection, models, ".", "data_mysql", "identity")


def test_unique_refuses_foreign_key_dependencies(monkeypatch):
    connection, models = _review_setup(monkeypatch, fk=True)
    with pytest.raises(ValueError, match="Foreign-key dependencies"):
        unique.review_unique_change(connection, models, ".", "data_mysql", "identity")


def test_unique_refuses_other_indexes(monkeypatch):
    connection, models = _review_setup(monkeypatch, indexes=("PRIMARY", "other_index"))
    with pytest.raises(ValueError, match="Other indexes"):
        unique.review_unique_change(connection, models, ".", "data_mysql", "identity")


def test_unique_drop_requires_exact_aurora_name(monkeypatch):
    connection, models = _review_setup(monkeypatch, enable=False, installed=("category",))
    operation, _, _ = unique.review_unique_change(connection, models, ".", "data_mysql", "identity")
    assert operation["kind"] == "drop_unique"
    assert "DROP INDEX" in operation["sql"]
    monkeypatch.setattr(unique, "_constraint_columns", lambda *args: None)
    with pytest.raises(ValueError, match="exact Aurora-owned"):
        unique.review_unique_change(connection, models, ".", "data_mysql", "identity")


def test_unique_identifier_length_is_portable():
    assert unique._constraint_name("item", "category") == "aurora_uq_item_category"
    with pytest.raises(ValueError, match="portable length"):
        unique._constraint_name("very_long_table_name_" * 3, "category")


def test_unique_review_hash_binds_identity(monkeypatch):
    connection, models = _review_setup(monkeypatch)
    _, one, _ = unique.review_unique_change(connection, models, ".", "data_mysql", "identity1")
    _, two, _ = unique.review_unique_change(connection, models, ".", "data_mysql", "identity2")
    assert one != two


def test_only_single_unique_flag_change_selects_dedicated_review():
    new, old = _models_and_snapshot()
    assert unique.is_single_unique_model_delta(old["models"], [new])
    both = deepcopy(new)
    both["columns"][0]["unique"] = True
    assert not unique.is_single_unique_model_delta(old["models"], [both])
    changed = deepcopy(new)
    changed["columns"][0]["nullable"] = True
    assert not unique.is_single_unique_model_delta(old["models"], [changed])
