"""Contract tests for R19 column-operation dependency structure."""

import ast
from pathlib import Path


def test_column_operations_has_no_module_level_import_islands():
    root = Path(__file__).resolve().parents[2]
    path = root / "aurora/database/column_operations.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    seen_nonimport = False
    late = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if seen_nonimport:
                late.append(node.lineno)
        elif not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
            and not seen_nonimport
        ):
            seen_nonimport = True
    assert late == []


def test_column_operation_imports_preserve_acyclic_eager_graph():
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    script = root / "scripts/source_structure.py"
    spec = importlib.util.spec_from_file_location("_r19_source_structure", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.assess(root)
    assert result["eager_dependency_cycles"] == []
    assert result["late_import_count"] == 12
