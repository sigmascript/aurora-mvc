"""Server successor post-DDL verification recognizes only a valid backup catalog."""

from types import SimpleNamespace

import pytest

from aurora.database import server_initial_verification as verifier
from aurora.database.models import Model, column


class Item(Model):
    __table__ = "server_backup_acceptance_item"
    id = column(int, primary_key=True)
    note = column(str, length=80, not_null=False)


@pytest.mark.parametrize("backend", ["mysql", "postgresql"])
@pytest.mark.parametrize("catalog", ["valid", "absent", "invalid"])
def test_server_schema_verifier_internal_backup_catalog(monkeypatch, backend, catalog):
    """The migration verifier must not mistake a verified backup catalog for drift."""
    objects = ["server_backup_acceptance_item", "__migrations__"]
    if catalog != "absent":
        objects.append("__backups__")
    if backend == "mysql":
        rows = [{"name": name, "kind": "BASE TABLE"} for name in objects]
    else:
        rows = [{"name": name, "kind": "r"} for name in objects]
    connection = SimpleNamespace(dialect=backend, fetch_all=lambda *_args, **_kw: rows)
    monkeypatch.setattr(verifier, "validate_init_models", lambda models, **_kw: list(models))
    observed = SimpleNamespace(name=Item.__table_name__, columns=[], indexes=[], unique_columns=[])
    monkeypatch.setattr(verifier, "_verify_declared_table", lambda *_args, **_kw: observed)
    from aurora.database import server_backup_catalog

    def catalog_format(_connection):
        if catalog == "invalid":
            raise ValueError("Conflicting __backups__ table definition")
        return "current"

    monkeypatch.setattr(server_backup_catalog, "_catalog_format", catalog_format)
    if catalog == "invalid":
        with pytest.raises(ValueError, match="Conflicting __backups__"):
            verifier.verify_server_declared_schema(connection, [Item], history_tables={"__migrations__"})
    else:
        assert verifier.verify_server_declared_schema(
            connection, [Item], history_tables={"__migrations__"})
