"""Fresh-interpreter initialization contracts for the guarded database dependency group.

A static import cycle involving late imports is not necessarily a runtime cycle.
Each scenario starts in a separate interpreter to avoid caching in sys.modules.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

MODULES = (
    "constraints",
    "initialization",
    "migration_history",
    "migration_review",
    "migration_safety",
    "schema_inspection",
    "schema_planning",
    "table_operations",
)


@pytest.mark.parametrize(
    "order",
    [
        MODULES,
        tuple(reversed(MODULES)),
        (
            "schema_inspection",
            "migration_review",
            "migration_history",
            "table_operations",
            "migration_safety",
            "schema_planning",
            "constraints",
            "initialization",
        ),
    ],
    ids=("source-order", "reverse-order", "inspection-first"),
)
def test_database_modules_initialize_in_fresh_interpreter(order, tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = (
        "import importlib, json, sys\n"
        "sys.path.insert(0, json.loads(sys.argv[1]))\n"
        "order = json.loads(sys.argv[2])\n"
        "for name in order:\n"
        "    importlib.import_module('aurora.database.' + name)\n"
        "from aurora.database import migration_history, migration_review, schema_inspection\n"
        "assert callable(migration_history.read_history)\n"
        "assert callable(migration_review.snapshot_fingerprint)\n"
        "assert callable(schema_inspection.inspect_table)\n"
    )
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, json.dumps(str(root)), json.dumps(order)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, (
        f"Fresh interpreter failed in order {order!r}:\n{result.stdout}\n{result.stderr}"
    )
