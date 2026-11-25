"""Behavioral checks for the read-only database dependency report."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "source_structure.py"


def _load():
    spec = importlib.util.spec_from_file_location("_source_structure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_module_level_import_islands_ignore_function_local_imports():
    inspect_source = _load().inspect_source
    source = '''"""Purpose."""
from .alpha import one
VALUE = 1
from .beta import two

def worker():
    from .gamma import three
    return three
'''
    result = inspect_source("aurora/database/sample.py", source)
    assert result["has_docstring"]
    assert [item["line"] for item in result["late_imports"]] == [4]
    assert result["late_imports"][0]["targets"] == ["aurora.database.beta"]


def test_cycle_detection_is_conservative_and_deterministic():
    cycles = _load().strongly_connected(
        {
            "a": {"b"},
            "b": {"c"},
            "c": {"a"},
            "d": {"e"},
            "e": set(),
            "f": {"f"},
        }
    )
    assert cycles == [["f"], ["a", "b", "c"]]


def test_real_aurora_database_source_is_scanned_read_only():
    module = _load()
    root = Path(__file__).resolve().parents[2]
    before = (root / "aurora/database/migration_history.py").read_bytes()
    report = module.assess(root)
    after = (root / "aurora/database/migration_history.py").read_bytes()
    assert before == after
    assert report["module_count"] >= 25
    assert report["late_import_count"] > 0
    assert any(row["path"] == "aurora/database/migration_history.py" for row in report["modules"])


def test_late_imports_do_not_falsely_count_as_eager_cycles(tmp_path):
    database = tmp_path / "aurora" / "database"
    database.mkdir(parents=True)
    (database / "alpha.py").write_text(
        '"""Alpha."""\nfrom .beta import feature\n', encoding="utf-8"
    )
    (database / "beta.py").write_text(
        '"""Beta."""\ndef feature():\n    return True\nfrom .alpha import feature\n',
        encoding="utf-8",
    )
    report = _load().assess(tmp_path)
    assert report["dependency_cycles"] == [["aurora.database.alpha", "aurora.database.beta"]]
    assert report["eager_dependency_cycles"] == []


def test_real_aurora_eager_graph_is_separate_from_deferred_graph():
    report = _load().assess(Path(__file__).resolve().parents[2])
    assert "eager_dependency_cycles" in report
    assert set(map(tuple, report["eager_dependency_cycles"])) <= set(
        map(tuple, report["dependency_cycles"])
    )
    assert report["late_import_count"] > 0


def test_eager_graph_has_no_cycles_after_reviewed_import_relocation():
    report = _load().assess(Path(__file__).resolve().parents[2])
    assert report["eager_dependency_cycles"] == []


def test_migration_execution_has_one_module_docstring_only():
    import ast

    root = Path(__file__).resolve().parents[2]
    tree = ast.parse(
        (root / "aurora/database/migration_execution.py").read_text(encoding="utf-8-sig")
    )
    strings = [
        node
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ]
    assert len(strings) == 1
    assert strings[0] is tree.body[0]


def test_reviewed_modules_have_no_late_import_islands():
    root = Path(__file__).resolve().parents[2]
    for name in ("cli_migrate", "migration_execution", "table_operations"):
        path = root / "aurora/database" / (name + ".py")
        info = _load().inspect_source(
            "aurora/database/" + name + ".py", path.read_text(encoding="utf-8-sig")
        )
        assert info["late_imports"] == [], name
