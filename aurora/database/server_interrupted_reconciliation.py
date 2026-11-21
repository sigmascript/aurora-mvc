"""Explicit history-only repair for one proven, already applied MySQL column.

Never executes DDL, never manufactures an unverified predecessor, and never
runs automatically after a failed migration.
"""

from datetime import datetime, timezone
from pathlib import Path

from aurora.core.project_policy import project_is_production

from .coordination import migration_lock
from .server_forward_history import (TABLE, _physical, _verify_table,
                                     history_table, read_history, _directory)
from .server_forward_init import _write_snapshot
from .server_initial_verification import verify_server_declared_schema
from .snapshots import schema_snapshot


def verify_interrupted_addition(connection, root, database, models, target):
    """Prove precisely one nullable plain column was added since latest history."""
    if connection.dialect != 'mysql' or history_table(connection) != TABLE:
        raise RuntimeError('Recovery requires MySQL five-column migration history')
    _verify_table(connection, TABLE)
    directory = _directory(root, database)
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError('Recorded migration snapshot directory is unavailable')
    rows = connection.fetch_all(
        'SELECT id,title,snapshot,date,warnings FROM __migrations__ ORDER BY id')
    if not rows or [row['id'] for row in rows] != list(range(1, len(rows) + 1)):
        raise RuntimeError('Migration history is incomplete or out of sequence')
    # Temporarily verify predecessor against a known snapshot rather than
    # calling read_history, which correctly refuses current physical drift.
    import json
    from .server_forward_history import _FILE
    seen = set()
    predecessor = None
    for row in rows:
        name = row['snapshot']
        if (not isinstance(name, str) or not _FILE.fullmatch(name)
                or name in seen or not row['title'] or not row['date']):
            raise RuntimeError('Migration history has invalid evidence')
        seen.add(name)
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError('Migration snapshot evidence is missing')
        try:
            entry = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError) as exc:
            raise RuntimeError('Migration snapshot evidence cannot be read') from exc
        if (not isinstance(entry, dict) or entry.get('database') != database
                or entry.get('engine') != 'mysql'
                or entry.get('title') != row['title']
                or not isinstance(entry.get('models'), list)
                or not isinstance(entry.get('physical_schema'), list)):
            raise RuntimeError('Migration snapshot evidence is inconsistent')
        predecessor = entry
    # Preserve and refuse unrecorded files (including partial prior recovery).
    if {p.name for p in directory.iterdir()} != seen:
        raise RuntimeError('Unrecorded migration snapshot evidence requires manual review')
    if len(rows) != 1:
        raise RuntimeError('Automatic history-only repair is limited to the first successor')

    table_name, separator, column_name = target.partition('.')
    if not separator or not table_name or not column_name:
        raise ValueError('Recovery target must be TABLE.COLUMN')
    declarations = schema_snapshot(*models, dialect='mysql', allow_v2=True)['tables']
    previous_models = predecessor['models']
    if (len(declarations) != len(previous_models)
            or {m['name'] for m in declarations} != {m['name'] for m in previous_models}):
        raise RuntimeError('Model table identity differs from predecessor')
    old_by_name = {m['name']: m for m in previous_models}
    for current in declarations:
        old = old_by_name[current['name']]
        if current['name'] == table_name:
            old_columns = old['columns']
            new_columns = current['columns']
            if (len(new_columns) != len(old_columns) + 1
                    or new_columns[:-1] != old_columns
                    or new_columns[-1]['name'] != column_name):
                raise RuntimeError('Model change is not one appended column')
            added = new_columns[-1]
            if (not added['nullable'] or added['primary_key'] or added['unique']
                    or added['references'] is not None or added['server_default'] is not None
                    or added['python_default'] != {'kind': 'literal', 'value': None}):
                raise RuntimeError('Recovery requires a plain nullable new column')
        elif current != old:
            raise RuntimeError('Unrelated model declarations differ from predecessor')

    # Native catalog verification includes triggers, indexes, keys, defaults,
    # exact types, and the independently validated internal backup catalog.
    verify_server_declared_schema(connection, models, history_tables={TABLE})
    before = predecessor['physical_schema']
    now = _physical(connection)
    if (len(now) != len(before)
            or [x['table'] for x in now] != [x['table'] for x in before]):
        raise RuntimeError('Physical table identities differ from predecessor')
    for current, old in zip(now, before):
        if current['table'] != table_name:
            if current != old:
                raise RuntimeError('Unrelated physical schema differs from predecessor')
            continue
        expected = {**old, 'columns': old['columns'] + current['columns'][-1:]}
        if (len(current['columns']) != len(old['columns']) + 1
                or [item[0] for item in current['columns'][:-1]] !=
                [item[0] for item in old['columns']]
                or current['columns'][-1][0] != column_name
                or current != expected):
            raise RuntimeError('Physical drift is not exactly the declared column')
    return rows


def _verify_backups(connection, root, database, url):
    """Read-only independent manifest/catalog integrity before history recovery."""
    from .backup_artifacts import destination_fingerprint, list_artifacts
    from .server_backup_catalog import inspect_server_catalog
    fingerprint = destination_fingerprint(url)
    reports = list_artifacts(root, database, fingerprint)
    reports = inspect_server_catalog(connection, root, database, fingerprint, reports)
    if any(report['status'] != 'ARTIFACT_VERIFIED' for report in reports):
        raise RuntimeError('Existing backup evidence is inconsistent')


def reconcile_interrupted_addition(connection, root, database, models, target, title,
                                   *, expected_identity, parsed, configured_url):
    """Perform history-only completion under the established migration lock."""
    from .cli_init import _mysql_database_identity

    if project_is_production(root):
        raise PermissionError('Production recovery is disabled')
    if connection._transaction_active or getattr(connection, '_migration_lock_active', False):
        raise RuntimeError('Recovery cannot nest transactions or locks')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('A recovery migration title is required')
    with migration_lock(connection):
        if _mysql_database_identity(connection, parsed) != expected_identity:
            raise RuntimeError('MySQL database identity changed before recovery')
        rows = verify_interrupted_addition(connection, root, database, models, target)
        _verify_backups(connection, root, database, configured_url)
        snapshot = _write_snapshot(connection, root, database, models, title)
        record = {
            'id': len(rows) + 1, 'title': title, 'snapshot': snapshot.name,
            'date': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'warnings': None,
        }
        try:
            connection.execute_write(
                'INSERT INTO __migrations__ (id,title,snapshot,date,warnings) '
                'VALUES (:id,:title,:snapshot,:date,:warnings)', record)
            verified, _ = read_history(connection, root, database)
            if (len(verified) != record['id']
                    or any(verified[-1][key] != value for key, value in record.items())):
                raise RuntimeError('Reconciled migration history could not be verified')
        except Exception as exc:
            raise RuntimeError(
                'History-only recovery outcome uncertain; preserve the new snapshot '
                'and inspect SQL history before any further action') from exc
        return snapshot
