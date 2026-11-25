"""R3.12: native catalog inspection has one read-only schema owner."""

import pytest

from aurora.database import migration_history as history
from aurora.database import schema_inspection as inspection


class CatalogConnection:
    def __init__(self, dialect, rows):
        self.dialect = dialect
        self.rows = rows
        self.statements = []

    def fetch_all(self, statement, params=None):
        self.statements.append((statement, params))
        return list(self.rows)

    def execute_write(self, *_args, **_kwargs):
        raise AssertionError("Catalog inspection must never execute writes")


def test_native_catalog_helpers_have_one_owner_and_preserve_compatibility():
    assert history._objects is inspection._objects
    assert history._physical is inspection._physical
    assert history.catalog_fingerprint is inspection.catalog_fingerprint


@pytest.mark.parametrize(
    "dialect, unexpected_kind",
    [("mysql", "VIEW"), ("postgresql", "v")],
)
def test_native_catalog_rejects_unsupported_objects_without_writes(
    dialect,
    unexpected_kind,
):
    connection = CatalogConnection(dialect, [{"name": "orphan", "kind": unexpected_kind}])
    with pytest.raises(RuntimeError, match="Unrecognized"):
        inspection._objects(connection)
    assert len(connection.statements) == 1


def test_native_catalog_refuses_unrecognized_backend_without_writes():
    connection = CatalogConnection("sqlite", [])
    with pytest.raises(ValueError, match="MySQL or PostgreSQL"):
        inspection._objects(connection)
    assert not connection.statements


def test_catalog_fingerprint_is_deterministic_and_rejects_nan():
    left = inspection.catalog_fingerprint("mysql", [{"b": 2, "a": 1}])
    right = history.catalog_fingerprint("mysql", [{"a": 1, "b": 2}])
    assert left == right
    assert len(left) == 64
    with pytest.raises(ValueError):
        inspection.catalog_fingerprint("mysql", [{"invalid": float("nan")}])
