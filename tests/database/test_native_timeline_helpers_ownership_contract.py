"""Native timeline helpers must be pure, deterministic and owned by timeline."""

import pytest

from aurora.database.migration_timeline import (
    canonical_operation_json,
    server_timeline_ddl,
)
from aurora.database.migration_timeline import (
    canonical_operation_json as legacy_canonical_operation_json,
)
from aurora.database.migration_timeline import (
    server_timeline_ddl as legacy_server_timeline_ddl,
)


@pytest.mark.parametrize(
    "dialect, expected_type",
    [
        ("mysql", "LONGTEXT"),
        ("postgresql", "TEXT"),
    ],
)
def test_native_ddl_is_pure_and_preserves_backend_sql(dialect, expected_type):
    assert legacy_server_timeline_ddl is server_timeline_ddl
    sql = server_timeline_ddl(dialect)
    assert sql.startswith("CREATE TABLE ")
    assert "aurorasql_migration_timeline" in sql
    assert f"models_json {expected_type} NOT NULL" in sql
    assert "'prepared', 'applied', 'uncertain'" in sql


def test_native_ddl_rejects_unsupported_dialect():
    with pytest.raises(ValueError, match="requires MySQL or PostgreSQL"):
        server_timeline_ddl("sqlite")


def test_native_operation_serialization_is_deterministic_and_fails_closed():
    assert legacy_canonical_operation_json is canonical_operation_json
    assert canonical_operation_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    with pytest.raises(ValueError):
        canonical_operation_json({"bad": float("nan")})
