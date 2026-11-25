"""Verify the MySQL FK-action normalization helpers keep their review semantics.

These definitions are intentionally local to the existing MySQL paths; moving
normalization to a shared import would add another database-module dependency.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

DATABASE = Path(__file__).resolve().parents[2] / "aurora" / "database"


def _local_function(module_name: str, function_name: str):
    tree = ast.parse((DATABASE / module_name).read_text(encoding="utf-8"))
    matching = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    ]
    assert len(matching) == 1
    function = matching[0]
    assert isinstance(function, ast.FunctionDef)
    namespace = {}
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(DATABASE / module_name), "exec"),
        namespace,
    )
    return namespace[function_name]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ([("NO ACTION", "NO ACTION")], [("RESTRICT", "RESTRICT")]),
        (
            [("CASCADE", "SET NULL"), ("RESTRICT", "NO ACTION")],
            [("CASCADE", "SET NULL"), ("RESTRICT", "RESTRICT")],
        ),
        ([], []),
    ],
)
def test_schema_planner_mysql_fk_actions(raw, expected):
    assert _local_function("schema_planning.py", "normalized")(raw) == expected


def test_schema_inspection_mysql_fk_action():
    normalize = _local_function("schema_inspection.py", "normalize")
    assert normalize("NO ACTION") == "RESTRICT"
    assert normalize("CASCADE") == "CASCADE"
    assert normalize("SET NULL") == "SET NULL"


def test_mysql_fk_normalization_does_not_use_assigned_lambdas():
    for module_name in ("schema_planning.py", "schema_inspection.py"):
        tree = ast.parse((DATABASE / module_name).read_text(encoding="utf-8"))
        assert not any(
            isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Lambda)
            for node in ast.walk(tree)
        )
