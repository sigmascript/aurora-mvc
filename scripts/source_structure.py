"""Read-only report of Aurora module import boundaries and dependency cycles.

This tool deliberately does not propose a safe automatic relocation. A cycle in
this conservative graph is a review lead, not proof of a runtime circular import:
imports may occur only after another module has finished initializing.
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import defaultdict
from pathlib import Path


def inspect_source(relative_path: str, source: str) -> dict:
    """Return ordered module-level import boundaries, excluding function-local imports."""
    tree = ast.parse(source, filename=relative_path)
    module = relative_path.removesuffix(".py").replace("/", ".")
    if module.endswith(".__init__"):
        module = module[:-9]
    seen_statement = False
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    package = (
                        module
                        if relative_path.endswith("/__init__.py")
                        else module.rpartition(".")[0]
                    )
                    pieces = package.split(".")
                    prefix = ".".join(pieces[: len(pieces) - node.level + 1])
                    target = prefix + ("." + node.module if node.module else "")
                else:
                    target = node.module or ""
                targets = [target]
            else:
                targets = [alias.name for alias in node.names]
            imports.append(
                {
                    "line": node.lineno,
                    "late": seen_statement,
                    "targets": targets,
                    "names": [alias.name for alias in node.names],
                    "relative": isinstance(node, ast.ImportFrom) and node.level > 0,
                }
            )
        elif not (
            not seen_statement
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            seen_statement = True
    return {
        "path": relative_path,
        "module": module,
        "has_docstring": bool(ast.get_docstring(tree)),
        "imports": imports,
        "late_imports": [entry for entry in imports if entry["late"]],
    }


def strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Find genuine directed dependency cycles in the conservative import graph."""
    index = 0
    indices = {}
    lows = {}
    stack = []
    active = set()
    components = []

    def visit(node):
        nonlocal index
        indices[node] = lows[node] = index
        index += 1
        stack.append(node)
        active.add(node)
        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                lows[node] = min(lows[node], lows[target])
            elif target in active:
                lows[node] = min(lows[node], indices[target])
        if lows[node] == indices[node]:
            group = []
            while True:
                member = stack.pop()
                active.remove(member)
                group.append(member)
                if member == node:
                    break
            if len(group) > 1 or node in graph[node]:
                components.append(sorted(group))

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return sorted(components, key=lambda group: (len(group), group))


def assess(root: Path) -> dict:
    """Inspect files without importing Aurora modules or opening databases."""
    base = root / "aurora" / "database"
    if not base.is_dir():
        raise ValueError("Expected aurora/database source directory")
    reports = []
    for path in sorted(base.glob("*.py")):
        if path.is_symlink():
            raise ValueError(f"Unexpected symlink: {path}")
        rel = path.relative_to(root).as_posix()
        reports.append(inspect_source(rel, path.read_text(encoding="utf-8-sig")))
    known = {record["module"] for record in reports}
    graph = defaultdict(set, {module: set() for module in known})
    eager_graph = defaultdict(set, {module: set() for module in known})
    for record in reports:
        for entry in record["imports"]:
            targets = {target for target in entry["targets"] if target in known}
            graph[record["module"]].update(targets)
            if not entry["late"]:
                eager_graph[record["module"]].update(targets)
    cycles = strongly_connected(dict(graph))
    eager_cycles = strongly_connected(dict(eager_graph))
    cycle_membership = {name: index for index, group in enumerate(cycles) for name in group}
    for record in reports:
        source = record["module"]
        for entry in record["late_imports"]:
            entry["cycle_candidate"] = any(
                target in cycle_membership
                and cycle_membership[target] == cycle_membership.get(source)
                for target in entry["targets"]
            )
    return {
        "module_count": len(reports),
        "late_import_count": sum(len(record["late_imports"]) for record in reports),
        "missing_module_docstrings": [
            record["path"] for record in reports if not record["has_docstring"]
        ],
        "dependency_cycles": cycles,
        "eager_dependency_cycles": eager_cycles,
        "modules": reports,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print complete read-only JSON report")
    args = parser.parse_args()
    report = assess(Path(__file__).resolve().parents[1])
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"Database source modules: {report['module_count']}")
    print(f"Module-level imports after statements: {report['late_import_count']}")
    print(f"Conservative dependency groups requiring review: {len(report['dependency_cycles'])}")
    print(f"Eager-import dependency cycles: {len(report['eager_dependency_cycles'])}")
    print(f"Modules missing purpose docstrings: {len(report['missing_module_docstrings'])}")
    for record in report["modules"]:
        entries = record["late_imports"]
        if entries:
            candidate = sum(bool(entry["cycle_candidate"]) for entry in entries)
            lines = ", ".join(str(item["line"]) for item in entries[:8])
            print(
                f"  {record['path']}: {len(entries)} late, {candidate} cycle candidates; lines {lines}"
            )
    for group in report["dependency_cycles"]:
        print("  REVIEW conservative dependency group: " + ", ".join(group))
    for group in report["eager_dependency_cycles"]:
        print("  EAGER IMPORT CYCLE: " + ", ".join(group))
    print("READ ONLY: no imports moved, warnings suppressed, or files modified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
