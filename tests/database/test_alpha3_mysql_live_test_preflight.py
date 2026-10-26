"""Offline checks for the read-only live-test eligibility helper."""
from __future__ import annotations

import pytest

from aurora.database.mysql_live_test_preflight import (
    APPROVED_SCHEMA, inspect_disposable_mysql_schema,
)


def test_rejects_unapproved_schema_before_connecting():
    with pytest.raises(PermissionError, match="approved"):
        inspect_disposable_mysql_schema("mysql+pymysql://user:pass@localhost/other", schema="other")


def test_rejects_wrong_url_database_before_connecting():
    with pytest.raises(PermissionError, match="Database URL"):
        inspect_disposable_mysql_schema("mysql+pymysql://user:pass@localhost/production")


def test_rejects_non_mysql_url_before_connecting():
    with pytest.raises(ValueError, match="mysql\\+pymysql"):
        inspect_disposable_mysql_schema("sqlite:///local.db")


def test_rejects_non_pymysql_driver_before_connecting():
    with pytest.raises(ValueError, match="mysql\\+pymysql"):
        inspect_disposable_mysql_schema("mysql+mysqldb://user:pass@localhost/" + APPROVED_SCHEMA)


def test_approved_schema_constant():
    assert APPROVED_SCHEMA == "aurora_alpha3_migration_test"
