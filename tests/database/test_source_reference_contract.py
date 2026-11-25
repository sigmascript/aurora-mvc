"""Regression checks for guarded server acceptance and recovery type references."""

import pytest

from aurora.database import migration_safety, schema_inspection
from aurora.database.migration_history import RecoveryAssessment


def test_recovery_preflight_declares_existing_assessment_type():
    # Resolve the forward annotation without introducing a runtime import cycle.
    assert migration_safety.RecoveryPreflight.__annotations__["assessment"] == "RecoveryAssessment"
    assert RecoveryAssessment.__name__ == "RecoveryAssessment"


@pytest.mark.parametrize(
    "url",
    [
        "mysql+pymysql://aurorasql_test@localhost/another_database",
        "postgresql+psycopg://wrong_user@localhost/aurorasql_test",
    ],
)
def test_server_acceptance_rejects_wrong_identity_before_connect(monkeypatch, url):
    from aurora.database import cli_check

    class Config:
        def resolve(self, alias):
            assert alias == "default"
            return url

        def connect(self, alias):
            pytest.fail("must reject mismatched acceptance identity before connecting")

    monkeypatch.setattr(cli_check, "_load_config", lambda config_path: (Config(), None))

    with pytest.raises(PermissionError, match="dedicated test database and user"):
        schema_inspection.inspect_server_acceptance_target("unused-config", "default")
