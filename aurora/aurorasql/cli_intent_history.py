"""Read-only CLI inspection of the guarded SQLite migration intent journal."""

import json
import sqlite3
from urllib.parse import urlsplit

from .cli_plan import _existing_sqlite_path, _load_config
from .migration_journal_v2 import list_intents


def run_intent_history(args):
    config, root = _load_config(args.config)
    url = config.resolve(args.database)
    if urlsplit(url).scheme not in ('sqlite', 'sqlite+pysqlite'):
        raise ValueError('SQLite intent history is unavailable for this backend')
    db_file = _existing_sqlite_path(url, root)
    raw = sqlite3.connect(db_file.as_uri() + '?mode=ro', uri=True, isolation_level=None)
    try:
        entries = list_intents(raw)
    finally:
        raw.close()
    report = {'backend': 'aurorasql', 'database_alias': args.database,
              'journal': 'sqlite_intents_v2', 'read_only': True,
              'entries': [{'migration_id': e.migration_id,
                           'review_sha256': e.review_sha256,
                           'before_sha256': e.before_sha256,
                           'after_sha256': e.after_sha256,
                           'state': e.state} for e in entries]}
    if args.format == 'json':
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"Database: {args.database} (AuroraSQL SQLite intent journal; read-only)")
        for entry in report['entries']:
            print(f"  {entry['migration_id']} {entry['state']}")
        print(f"Intent entries: {len(entries)}")
    return 3 if any(e.state != 'applied' for e in entries) else 0
