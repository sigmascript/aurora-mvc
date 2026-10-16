"""Explicit trusted model selection for database CLI commands.

The manifest never discovers Python modules; referenced project modules are imported
only when a model-aware command is explicitly invoked.
"""
from __future__ import annotations
import json
import re
from pathlib import Path

_REF = re.compile(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*$')
_ALIAS = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')


def select_model_refs(*, explicit=(), database='default', manifest='aurora_models.json'):
    """Return explicit refs if supplied, otherwise refs for the selected alias.

    Manifest format: {"databases": {"default": ["pkg.models:Model"]}}.
    No fallback across aliases; no automatic discovery.
    """
    if explicit:
        refs = list(explicit)
    else:
        path = Path(manifest).expanduser()
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError as exc:
            raise ValueError(f'No --model supplied and model manifest missing: {path}') from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'Cannot read model manifest: {path}: {exc}') from exc
        if (
            not isinstance(raw, dict)
            or set(raw) != {'databases'}
            or not isinstance(raw['databases'], dict)
        ):
            raise ValueError('Model manifest must contain only a databases mapping')
        mapping = raw['databases']
        for alias, items in mapping.items():
            if (
                not isinstance(alias, str)
                or not _ALIAS.fullmatch(alias)
                or not isinstance(items, list)
            ):
                raise ValueError('Invalid model manifest database entry')
            if not items or any(
                not isinstance(item, str) or not _REF.fullmatch(item)
                for item in items
            ):
                raise ValueError(f'Invalid model references for database {alias}')
            if len(set(items)) != len(items):
                raise ValueError(f'Duplicate model references for database {alias}')
        if database not in mapping:
            raise ValueError(f'No models registered for database alias: {database}')
        refs = mapping[database]
    if not refs or any(
        not isinstance(ref, str) or not _REF.fullmatch(ref)
        for ref in refs
    ):
        raise ValueError('Models must be explicit MODULE:CLASS references')
    if len(set(refs)) != len(refs):
        raise ValueError('Duplicate model references')
    return tuple(refs)
