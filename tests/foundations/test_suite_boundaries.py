"""Ensure the active suite stays isolated from historical test modules."""

from pathlib import Path


def test_active_suite_does_not_import_legacy_modules():
    root = Path(__file__).resolve().parents[1]
    for path in root.rglob("*.py"):
        if path.name == __file__.split("/")[-1]:
            continue
        text = path.read_text(encoding="utf-8")
        import ast
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("tests_legacy"), str(path)
            elif isinstance(node, ast.Import):
                assert all(not alias.name.startswith("tests_legacy") for alias in node.names), str(path)
