"""Safe, explicit edits to Aurora's model manifest; no Python imports."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from .cli_model_manifest import _ALIAS, _REF


def _load(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError('Refusing symlink model manifest')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f'Cannot read model manifest: {exc}') from exc
    if (
        not isinstance(data, dict)
        or set(data) != {'databases'}
        or not isinstance(data['databases'], dict)
    ):
        raise ValueError('Model manifest must contain only a databases mapping')
    for alias, refs in data['databases'].items():
        if (
            not isinstance(alias, str)
            or not _ALIAS.fullmatch(alias)
            or not isinstance(refs, list)
            or any(not isinstance(ref, str) or not _REF.fullmatch(ref) for ref in refs)
            or len(refs) != len(set(refs))
        ):
            raise ValueError('Invalid model manifest database entry')
    return data


def registered_models(*, manifest='aurora_models.json', database='default') -> tuple[str, ...]:
    if not _ALIAS.fullmatch(database):
        raise ValueError('Invalid database alias')
    return tuple(_load(Path(manifest).expanduser())['databases'].get(database, ()))


def register_model(reference: str, *, manifest='aurora_models.json', database='default') -> bool:
    """Add one model reference, atomically replacing a regular manifest file.

    Return False when already present. No imports, no database operations.
    """
    if not isinstance(reference, str) or not _REF.fullmatch(reference):
        raise ValueError('Expected a MODULE:CLASS model reference')
    if not _ALIAS.fullmatch(database):
        raise ValueError('Invalid database alias')
    path = Path(manifest).expanduser()
    data = _load(path)
    if not path.is_file():
        raise ValueError('Model manifest must be a regular file')
    refs = data['databases'].setdefault(database, [])
    if reference in refs:
        return False
    refs.append(reference)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=path.parent,
            prefix='.aurora-models-',
            suffix='.tmp',
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            json.dump(data, tmp, indent=2)
            tmp.write('\n')
            tmp.flush()
            os.fsync(tmp.fileno())
        if path.is_symlink():
            raise ValueError('Refusing symlink model manifest')
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return True
