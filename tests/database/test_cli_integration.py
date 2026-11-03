"""End-to-end reviewed SQLite CLI execution and journal inspection."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aurora.database.migration_journal_v2 import initialize_intent_journal
from aurora.database.review_artifacts import create_review, save_review
from aurora.database.schema import create_table_sql
from aurora.database.snapshots import schema_snapshot
from aurora.database.models import Model, column

ROOT = Path(__file__).resolve().parents[2]


class Before(Model):
    __table__ = 'notes'
    id = column(int, primary_key=True)
    title = column(str)


class After(Model):
    __table__ = 'notes'
    id = column(int, primary_key=True)
    title = column(str)
    description = column(str, nullable=True)


class Tags(Model):
    __table__ = 'tags'
    id = column(int, primary_key=True)
    label = column(str)


def cli(project, *args, env=None):
    variables = dict(os.environ)
    variables['PYTHONPATH'] = str(ROOT) + os.pathsep + str(project)
    if env:
        variables.update(env)
    return subprocess.run([sys.executable, '-m', 'aurora', 'db', *args],
                          cwd=project, env=variables, text=True, capture_output=True, timeout=30)


@pytest.fixture
def project(tmp_path):
    (tmp_path / 'appmodels.py').write_text(
        'from aurora.database.models import Model, column\n'
        'class Note(Model):\n'
        '    __table__ = "notes"\n'
        '    id = column(int, primary_key=True)\n'
        '    title = column(str)\n'
        '    description = column(str, nullable=True)\n'
        'class Tag(Model):\n'
        '    __table__ = "tags"\n'
        '    id = column(int, primary_key=True)\n'
        '    label = column(str)\n', encoding='utf-8')
    (tmp_path / 'aurora_databases.json').write_text(json.dumps({
        'databases': {'default': {'url': 'sqlite:///data.sqlite'}}}), encoding='utf-8')
    db = sqlite3.connect(tmp_path / 'data.sqlite')
    db.execute('PRAGMA foreign_keys=ON')
    initialize_intent_journal(db, confirm=True)
    db.execute(create_table_sql(Before, 'sqlite'))
    db.execute('INSERT INTO notes(title) VALUES (?)', ('keep',))
    db.commit()
    db.close()
    review = create_review(schema_snapshot(Before, dialect='sqlite'),
                           schema_snapshot(After, Tags, dialect='sqlite'),
                           migration_id='AddDescriptionAndTags')
    save_review(review, tmp_path / 'review.json')
    return tmp_path, review


def apply(project, digest, *extra, env=None):
    return cli(project, 'migrate', '--review-file', 'review.json', '--apply',
               '--confirm', 'APPLY', '--approve', digest,
               '--model', 'appmodels:Note', '--model', 'appmodels:Tag', *extra, env=env)


def test_reviewed_cli_additive_and_intent_history(project):
    root, review = project
    result = apply(root, review['review_sha256'])
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(root / 'data.sqlite') as db:
        assert db.execute('SELECT title, description FROM notes').fetchone() == ('keep', None)
        assert db.execute('SELECT name FROM sqlite_master WHERE name="tags"').fetchone()
    history = cli(root, 'history', '--intents', '--format', 'json')
    assert history.returncode == 0, history.stderr
    parsed = json.loads(history.stdout)
    assert parsed['journal'] == 'sqlite_intents_v2'
    assert parsed['entries'][0]['state'] == 'applied'
    assert apply(root, review['review_sha256']).returncode != 0


def test_reviewed_cli_rejects_wrong_digest_without_writes(project):
    root, _ = project
    result = apply(root, '0' * 64)
    assert result.returncode != 0
    with sqlite3.connect(root / 'data.sqlite') as db:
        assert db.execute('SELECT count(*) FROM notes').fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM sqlite_master WHERE name="tags"').fetchone()


def test_reviewed_cli_rejects_production_and_preview_mixture(project):
    root, review = project
    assert apply(root, review['review_sha256'], env={'AURORA_ENV': 'production'}).returncode != 0
    assert apply(root, review['review_sha256'], '--dry-run').returncode != 0
    assert cli(root, 'migrate', '--review-file', 'review.json').returncode != 0


def test_intent_history_missing_journal_fails_closed(project):
    root, _ = project
    with sqlite3.connect(root / 'data.sqlite') as db:
        db.execute('DROP TABLE aurorasql_migration_intents_v2')
    assert cli(root, 'history', '--intents').returncode != 0


def test_reviewed_cli_rejects_unexpected_schema(project):
    root, review = project
    with sqlite3.connect(root / 'data.sqlite') as db:
        db.execute('CREATE INDEX unexpected ON notes(title)')
    assert apply(root, review['review_sha256']).returncode != 0
    with sqlite3.connect(root / 'data.sqlite') as db:
        assert not db.execute('SELECT 1 FROM sqlite_master WHERE name="tags"').fetchone()
        assert not db.execute('SELECT 1 FROM aurorasql_migration_intents_v2').fetchone()


def test_reviewed_cli_initial_create_tables(tmp_path):
    (tmp_path / 'appmodels.py').write_text(
        'from aurora.database.models import Model, column\n'
        'class Note(Model):\n'
        '    __table__ = "notes"\n'
        '    id = column(int, primary_key=True)\n'
        '    title = column(str)\n', encoding='utf-8')
    (tmp_path / 'aurora_databases.json').write_text(json.dumps({
        'databases': {'default': {'url': 'sqlite:///data.sqlite'}}}), encoding='utf-8')
    with sqlite3.connect(tmp_path / 'data.sqlite') as db:
        initialize_intent_journal(db, confirm=True)
    before = {'format': 'aurorasql.schema.v1', 'dialect': 'sqlite', 'tables': []}
    review = create_review(before, schema_snapshot(Before, dialect='sqlite'),
                           migration_id='CreateNotes')
    save_review(review, tmp_path / 'review.json')
    result = cli(tmp_path, 'migrate', '--review-file', 'review.json', '--apply',
                 '--confirm', 'APPLY', '--approve', review['review_sha256'],
                 '--model', 'appmodels:Note')
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(tmp_path / 'data.sqlite') as db:
        assert db.execute('SELECT name FROM sqlite_master WHERE name="notes"').fetchone()
