"""A model table rename requires stable identity and no table collision."""

from argparse import Namespace
import json
import sqlite3

import pytest

from aurora.database import Model, column
from aurora.database.cli_init import _execute_batch
from aurora.database.cli_plan import run_plan
from aurora.database.core import Connection
from aurora.database.model_identity import model_identity_map, table_rename_candidates


def _model(name, table, *, module="project.models"):
    return type(name, (Model,), {"__module__": module, "__table__": table,
                                "id": column(int, primary_key=True)})


def test_same_model_identity_can_propose_one_table_rename():
    original = _model("Customer", "customers")
    renamed = _model("Customer", "clients")
    previous = model_identity_map((original,))
    assert table_rename_candidates(previous, (renamed,)) == (
        ("project.models:Customer", "customers", "clients"),
    )


def test_new_model_and_removed_model_do_not_imply_table_rename():
    original = _model("Customer", "customers")
    separate = _model("Client", "clients")
    assert table_rename_candidates(model_identity_map((original,)), (separate,)) == ()


def test_table_collision_blocks_inferred_rename():
    customer = _model("Customer", "customers")
    account = _model("Account", "accounts")
    renamed = _model("Customer", "accounts")

    with pytest.raises(ValueError, match="ambiguous"):
        table_rename_candidates(model_identity_map((customer, account)), (renamed,))


def test_duplicate_destination_model_names_are_rejected():
    customer = _model("Customer", "accounts")
    account = _model("Account", "accounts")

    with pytest.raises(ValueError, match="Duplicate"):
        model_identity_map((customer, account))


def test_unstable_local_model_identity_does_not_authorize_rename():
    class Local(Model):
        __table__ = "old_local"
        id = column(int, primary_key=True)

    class NewLocal(Model):
        __table__ = "new_local"
        id = column(int, primary_key=True)

    assert table_rename_candidates(model_identity_map((Local,)), (NewLocal,)) == ()


def test_read_only_plan_reports_trusted_table_rename_candidate(tmp_path, capsys):
    database = tmp_path / "schema.sqlite3"
    original = _model("Customer", "customers", module="customer_schema_for_rename")

    with Connection(sqlite3.connect(database, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (original,))

    (tmp_path / "customer_schema_for_rename.py").write_text(
        'from aurora.database import Model, column\n'
        'class Customer(Model):\n'
        '    __table__ = "clients"\n'
        '    id = column(int, primary_key=True)\n', encoding="utf-8")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({
        "databases": {"default": {"url": "sqlite:///schema.sqlite3"}}
    }), encoding="utf-8")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({
        "databases": {"default": ["customer_schema_for_rename:Customer"]}
    }), encoding="utf-8")
    before = database.read_bytes()
    args = Namespace(config=str(config), database="default", models_config=str(manifest),
                     model=[], format="json", check=False)

    assert run_plan(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["table_rename_candidates"] == [{
        "model": "customer_schema_for_rename:Customer", "from": "customers", "to": "clients",
        "execution_allowed": False,
    }]
    assert database.read_bytes() == before


def test_plan_flags_corrupt_native_history_before_claiming_verified(tmp_path, capsys):
    database = tmp_path / "history.sqlite3"
    original = _model("Customer", "customers", module="historical_customer")

    with Connection(sqlite3.connect(database, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (original,))

    (tmp_path / "historical_customer.py").write_text(
        'from aurora.database import Model, column\n'
        'class Customer(Model):\n'
        '    __table__ = "customers"\n'
        '    id = column(int, primary_key=True)\n', encoding="utf-8")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///history.sqlite3"}}}), encoding="utf-8")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {"default": [
        "historical_customer:Customer"]}}), encoding="utf-8")

    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE customers ADD COLUMN unexpected TEXT")

    args = Namespace(config=str(config), database="default", models_config=str(manifest),
                     model=[], format="json", check=True)
    assert run_plan(args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["native_history_verified"] is False
    assert report["advisory_only"] is True


def test_removing_model_registration_never_plans_table_deletion(tmp_path, capsys):
    database = tmp_path / "registered.sqlite3"
    customer = _model("Customer", "customers", module="registered_customers")
    article = _model("Article", "articles", module="registered_customers")

    with Connection(sqlite3.connect(database, isolation_level=None), "sqlite") as connection:
        _execute_batch(connection, (customer, article))

    (tmp_path / "registered_customers.py").write_text(
        'from aurora.database import Model, column\n'
        'class Customer(Model):\n'
        '    __table__ = "customers"\n'
        '    id = column(int, primary_key=True)\n', encoding="utf-8")
    config = tmp_path / "aurora_databases.json"
    config.write_text(json.dumps({"databases": {"default": {
        "url": "sqlite:///registered.sqlite3"}}}), encoding="utf-8")
    manifest = tmp_path / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {"default": [
        "registered_customers:Customer"]}}), encoding="utf-8")
    args = Namespace(config=str(config), database="default", models_config=str(manifest),
                     model=[], format="json", check=True)

    assert run_plan(args) == 3
    report = json.loads(capsys.readouterr().out)
    assert report["unregistered_history_models"] == ["registered_customers:Article"]
    assert report["native_history_verified"] is True
    assert not any(change["kind"] == "removed_table" for item in report["models"]
                   for change in item["changes"])

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='articles'"
        ).fetchone() == ("articles",)
