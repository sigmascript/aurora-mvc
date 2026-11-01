"""Read-only evidence tests; no migration execution permission is issued."""

import sqlite3
import unittest

from aurora.aurorasql.core import Connection
from aurora.aurorasql.live_schema_evidence import inspect_reviewed_baseline
from aurora.aurorasql.migration_format import snapshot_fingerprint


def snapshot(*columns, dialect="sqlite"):
    return {"format": "aurorasql.schema.v1", "dialect": dialect, "tables": [{
        "name": "items", "columns": [{
            "name": name, "type": "int", "length": None, "primary_key": name == "id",
            "nullable": name != "id", "unique": False, "references": None,
            "python_default": {"kind": "literal", "value": None},
            "server_default": None,
        } for name in columns],
    }]}


class LiveSchemaEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.raw = sqlite3.connect(":memory:")
        self.connection = Connection(self.raw, "sqlite")

    def tearDown(self):
        self.connection.close()

    def test_matching_columns_still_do_not_authorize_execution(self):
        self.raw.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value INTEGER)")
        before = snapshot("id", "value")
        evidence = inspect_reviewed_baseline(self.connection, before)
        self.assertTrue(evidence.structurally_consistent)
        self.assertFalse(evidence.execution_allowed)
        self.assertEqual(evidence.reviewed_before_sha256, snapshot_fingerprint(before))

    def test_missing_table(self):
        evidence = inspect_reviewed_baseline(self.connection, snapshot("id"))
        self.assertEqual(evidence.findings, ("Missing table: items",))

    def test_missing_and_extra_columns(self):
        self.raw.execute("CREATE TABLE items (id INTEGER, other INTEGER)")
        evidence = inspect_reviewed_baseline(self.connection, snapshot("id", "value"))
        self.assertEqual(evidence.findings, ("Missing column: items.value", "Unexpected column: items.other"))

    def test_dialect_mismatch_fails_closed(self):
        with self.assertRaises(ValueError):
            inspect_reviewed_baseline(self.connection, snapshot("id", dialect="mysql"))

    def test_invalid_snapshot_fails_closed(self):
        with self.assertRaises(ValueError):
            inspect_reviewed_baseline(self.connection, {"dialect": "sqlite", "tables": []})


if __name__ == "__main__":
    unittest.main()
