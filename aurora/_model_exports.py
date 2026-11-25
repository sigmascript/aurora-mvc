"""Safely maintain only Aurora-owned package-level model exports."""

from __future__ import annotations

import ast
import os
import tempfile
from pathlib import Path

_BEGIN = "# Aurora model exports (managed; do not edit inside this block)"
_END = "# End Aurora model exports"


def plan_model_export(
    folder: Path, name: str, *, remove: bool = False
) -> tuple[Path, bytes, bytes]:
    """Plan an export without importing project code or changing any files."""
    package = folder / "__init__.py"
    if folder.is_symlink() or package.is_symlink() or (package.exists() and not package.is_file()):
        raise ValueError("Model package path is unsafe; no files changed")
    if not folder.is_dir() and (remove or not folder.parent.is_dir()):
        raise ValueError("Model package directory does not exist")
    try:
        source = package.read_bytes().decode("utf-8") if package.is_file() else ""
    except (OSError, UnicodeError):
        raise ValueError("Cannot safely read model package") from None
    newline = "\r\n" if "\r\n" in source else "\n"
    if source.count(_BEGIN) != source.count(_END) or source.count(_BEGIN) > 1:
        raise ValueError("Model exports are customized; edit __init__.py manually")
    start = source.find(_BEGIN)
    end = source.find(_END)
    trailing = source[end + len(_END) :] if end >= 0 else ""
    if (start >= 0) != (end >= 0) or (
        start >= 0
        and (
            start > end
            or (start and source[start - 1] != "\n")
            or (trailing and not trailing.startswith(("\n", "\r\n")))
        )
    ):
        raise ValueError("Model exports are customized; edit __init__.py manually")
    block = ""
    names = []
    if start >= 0:
        block_end = end + len(_END)
        block = source[start:block_end]
        lines = block.splitlines()
        for line in lines[1:-1]:
            if not line.startswith("from .") or " import " not in line:
                raise ValueError("Model exports are customized; edit __init__.py manually")
            module, _, exported = line[len("from .") :].partition(" import ")
            if (
                not module.isidentifier()
                or not exported.isidentifier()
                or module != exported
                or line != f"from .{module} import {module}"
            ):
                raise ValueError("Model exports are customized; edit __init__.py manually")
            names.append(module)
        if names != sorted(set(names)):
            raise ValueError("Model exports are customized; edit __init__.py manually")
        external = source[:start] + source[block_end:]
    else:
        external = source

    # A custom binding of the same name must never be silently shadowed.
    try:
        tree = ast.parse(external)
    except SyntaxError:
        raise ValueError("Cannot safely inspect customized model package") from None
    for node in tree.body:
        bound = set()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for entry in node.names:
                bound.add(
                    entry.asname
                    or (entry.name.split(".")[0] if isinstance(node, ast.Import) else entry.name)
                )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                bound.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        if name in bound:
            raise ValueError(f"Model name '{name}' conflicts with a custom package export")

    if remove:
        if name not in names:
            # A custom import of the removed model cannot be managed implicitly.
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module == name:
                    raise ValueError("Model has a custom package import; remove it manually first")
            return package, source.encode("utf-8"), source.encode("utf-8")
        names.remove(name)
    else:
        if name in names:
            raise ValueError(f"Model '{name}' is already exported")
        names.append(name)
    names.sort()
    new_block = (
        (
            _BEGIN
            + newline
            + "".join(f"from .{item} import {item}{newline}" for item in names)
            + _END
        )
        if names
        else ""
    )
    if start >= 0:
        updated = source[:start] + new_block + source[end + len(_END) :]
        if not names:
            updated = updated.rstrip("\r\n") + newline if updated.strip() else ""
    else:
        updated = (
            source.rstrip("\r\n") + (newline * 2 if source.strip() else "") + new_block + newline
        )
    ast.parse(updated)
    return package, updated.encode("utf-8"), source.encode("utf-8")


def write_model_export(package: Path, content: bytes, *, expected: bytes | None = None) -> None:
    """Atomically replace one verified regular package file."""
    if package.is_symlink() or (package.exists() and not package.is_file()):
        raise ValueError("Model package changed; refusing export update")
    if expected is not None and (package.read_bytes() if package.exists() else b"") != expected:
        raise ValueError("Model package changed since export review; no files overwritten")
    temp = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=package.parent, prefix=".aurora-exports-", delete=False
        ) as stream:
            temp = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if package.is_symlink() or (package.exists() and not package.is_file()):
            raise ValueError("Model package changed; refusing export update")
        if expected is not None and (package.read_bytes() if package.exists() else b"") != expected:
            raise ValueError("Model package changed since export review; no files overwritten")
        os.replace(temp, package)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
