"""Guard stable Aurora exports and avoid shadowing the column constructor."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_history_recovery_local_names_do_not_shadow_column_constructor():
    source = (ROOT / "aurora/database/migration_history.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    names = {"_verify_table", "review_recovery_schema", "verify_server_applied_chain"}
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            assert not any(
                isinstance(child, ast.Name) and child.id == "column" for child in ast.walk(node)
            ), node.name


def test_public_aurora_alias_is_initialized_after_public_imports():
    source = (ROOT / "aurora/__init__.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    alias_position = next(
        i
        for i, node in enumerate(module.body)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "Aurora" for target in node.targets)
    )
    assert all(
        i < alias_position
        for i, node in enumerate(module.body)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    )
