"""Stage exact Python model edits for expressly requested JSON schema renames.

Model-only column additions/removals are never inferred as renames. An explicit
rename changes the source only after its exact resulting schema is reviewed.
"""

import ast
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
from unittest.mock import patch


class ModelSourceRenameError(ValueError):
    """Known, safe refusal of an explicit source rename."""


def _source_model(root, reference):
    module, cls = reference.split(":", 1)
    path = (Path(root) / Path(*module.split('.'))).with_suffix('.py')
    project = Path(root).resolve()
    if (path.is_symlink() or not path.is_file() or
            not path.resolve().is_relative_to(project)):
        raise ModelSourceRenameError(f"Model source is not a regular project file: {reference}")
    source = path.read_bytes()
    try:
        value = source.decode('utf-8')
        tree = ast.parse(value, filename=str(path))
    except (UnicodeError, SyntaxError) as error:
        raise ModelSourceRenameError(f"Cannot safely edit model source: {reference}") from error
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == cls]
    if len(classes) != 1:
        raise ModelSourceRenameError(f"Model class is missing or ambiguous: {reference}")
    return path, source, value, classes[0]


def _span(lines, node):
    # AST offsets are UTF-8 byte positions, not Python character positions.
    return (sum(len(line) for line in lines[:node.lineno - 1]) + node.col_offset,
            sum(len(line) for line in lines[:node.end_lineno - 1]) + node.end_col_offset)


def _edit_one(root, reference, operation, target_table, *, is_target, references):
    path, original, text, cls = _source_model(root, reference)
    lines = original.splitlines(keepends=True)
    replacements = []
    if is_target and operation['kind'] == 'rename_columns':
        found = set()
        for stmt in cls.body:
            if not (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and
                    isinstance(stmt.targets[0], ast.Name) and
                    isinstance(stmt.value, ast.Call) and
                    (isinstance(stmt.value.func, ast.Name) and stmt.value.func.id == 'column' or
                     isinstance(stmt.value.func, ast.Attribute) and stmt.value.func.attr == 'column')):
                continue
            name = stmt.targets[0].id
            if name in operation['columns']:
                start, end = _span(lines, stmt.targets[0])
                replacements.append((start, end, operation['columns'][name].encode()))
                found.add(name)
            if name in operation['columns'].values():
                raise ModelSourceRenameError(f"Rename destination {name} already exists in Python model")
        if found != set(operation['columns']):
            raise ModelSourceRenameError(f"Rename source column(s) not found as simple model declarations: {set(operation['columns']) - found}")
    elif is_target:
        # Keep the Python model class and path stable; only its physical table identity changes.
        mappings = [stmt for stmt in cls.body if isinstance(stmt, ast.Assign) and
                    len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and
                    stmt.targets[0].id == '__table__']
        if len(mappings) > 1:
            raise ModelSourceRenameError("Ambiguous __table__ declarations")
        if mappings:
            stmt = mappings[0]
            if not isinstance(stmt.value, ast.Constant) or stmt.value.value != target_table:
                raise ModelSourceRenameError("Existing __table__ does not match the rename source")
            start, end = _span(lines, stmt.value)
            replacements.append((start, end, repr(operation['new_table']).encode()))
        else:
            if not cls.body:
                raise ModelSourceRenameError("Cannot locate model class body")
            body = cls.body[0]
            # Keep an existing class docstring as the first statement.
            if (isinstance(body, ast.Expr) and isinstance(body.value, ast.Constant)
                    and isinstance(body.value.value, str)):
                if len(cls.body) < 2:
                    raise ModelSourceRenameError('Cannot safely insert table mapping after a standalone model docstring')
                body = cls.body[1]
            leading = lines[body.lineno - 1][:body.col_offset]
            if not leading or any(char not in b' \t' for char in leading):
                raise ModelSourceRenameError("Cannot safely insert explicit model table mapping")
            pos = sum(map(len, lines[:body.lineno - 1]))
            newline = b'\r\n' if b'\r\n' in original else b'\n'
            replacements.append((pos, pos, leading + b'__table__ = ' + repr(operation['new_table']).encode() + newline))
    for field_name, (old_reference, new_reference) in references.items():
        matches = [stmt for stmt in cls.body if isinstance(stmt, ast.Assign) and
                   len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and
                   stmt.targets[0].id == field_name and isinstance(stmt.value, ast.Call)]
        if len(matches) != 1:
            raise ModelSourceRenameError(f'Cannot identify dependent foreign-key field {field_name}')
        keywords = [kw for kw in matches[0].value.keywords if kw.arg in ('reference', 'references')]
        if (len(keywords) != 1 or not isinstance(keywords[0].value, ast.Constant) or
                keywords[0].value.value != old_reference):
            raise ModelSourceRenameError(f'Cannot safely rewrite foreign-key reference for {field_name}')
        start, end = _span(lines, keywords[0].value)
        replacements.append((start, end, repr(new_reference).encode()))
    changed = original
    for start, end, value in sorted(replacements, reverse=True):
        if not (0 <= start <= end <= len(changed)):
            raise ModelSourceRenameError('Invalid Python source position')
        changed = changed[:start] + value + changed[end:]
    if changed == original:
        raise ModelSourceRenameError("Explicit rename made no model-source change")
    try:
        ast.parse(changed.decode('utf-8'))
    except (SyntaxError, UnicodeError) as error:
        raise ModelSourceRenameError('Generated Python model source is invalid') from error
    return path, (original, changed)


def source_changes(root, refs, models, operation):
    target = [model for model in models if model.__table_name__.casefold() == operation['table'].casefold()]
    if len(target) != 1:
        raise ModelSourceRenameError('Rename target must correspond to exactly one registered Python model')
    target = target[0]
    by_name = {model.__name__: model for model in models}
    by_table = {model.__table_name__: model for model in models}
    changes = {}
    for model in models:
        identity = f'{model.__module__}:{model.__name__}'
        if identity not in refs:
            raise ModelSourceRenameError('Cannot identify the registered Python model file')
        references = {}
        for field_name, field in model.__columns__.items():
            if field.references is None:
                continue
            prefix, column = field.references.split('.')
            linked = by_name.get(prefix) if field.model_reference else by_table.get(prefix)
            if linked is target:
                if operation['kind'] == 'rename_table' and not field.model_reference:
                    updated = f"{operation['new_table']}.{column}"
                elif operation['kind'] == 'rename_columns' and column in operation['columns']:
                    updated = f"{prefix}.{operation['columns'][column]}"
                else:
                    continue
                references[field_name] = (field.references, updated)
        if model is target or references:
            path, pair = _edit_one(root, identity, operation, model.__table_name__,
                                   is_target=model is target, references=references)
            if path.resolve() in changes:
                raise ModelSourceRenameError('Several model identities target the same source file')
            changes[path.resolve()] = pair
    return changes


@contextmanager
def staged_model_source(changes):
    """Present proposed bytes to fresh project model loader without touching disk."""
    from .cli_plan import _FreshModelLoader
    original_get_data = _FreshModelLoader.get_data
    replacements = {str(path): new for path, (_, new) in changes.items()}

    def get_data(loader, path):
        return replacements.get(str(Path(path).resolve()), original_get_data(loader, path))

    with patch.object(_FreshModelLoader, 'get_data', get_data):
        yield


def _replace(path, content, mode):
    # Atomic per-file replacement, preserving existing permission bits.
    descriptor, temporary = tempfile.mkstemp(prefix='.aurora-rename-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_source_changes(changes):
    """Apply pre-reviewed source edits; return a reversible source-change witness."""
    applied = []
    try:
        for path, (before, after) in changes.items():
            if (path.is_symlink() or not path.is_file() or path.read_bytes() != before):
                raise ModelSourceRenameError('Model source was changed after rename review; retry with the current source')
            mode = stat.S_IMODE(path.stat().st_mode)
            _replace(path, after, mode)
            applied.append((path, before, after, mode))
    except BaseException:
        restore_source_changes(applied)
        raise
    return applied


def restore_source_changes(applied):
    for path, before, after, mode in reversed(applied):
        if path.is_symlink() or not path.is_file() or path.read_bytes() != after:
            raise RuntimeError(f'Cannot restore model source modified concurrently: {path}')
        _replace(path, before, mode)
