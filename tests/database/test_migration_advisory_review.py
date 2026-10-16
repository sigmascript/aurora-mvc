import contextlib
import io
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from aurora.cli import main
from aurora.database.cli_review import create_review, show_review, validate_plan
from aurora.database.plan import MigrationPlan, MigrationStep

class AdvisoryReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = MigrationPlan((MigrationStep('missing_table','example',None,None,'table','review','Inspect first'),))

    def test_validate_never_executable(self):
        review, ready = validate_plan(self.plan)
        self.assertFalse(review.executable)
        self.assertFalse(ready.executable)
        self.assertEqual(ready.status, 'blocked')

    def test_empty_plan_no_changes(self):
        review, ready = validate_plan(MigrationPlan(()))
        self.assertEqual(review.status, 'no_changes')
        self.assertEqual(ready.status, 'no_changes')

    def test_round_trip(self):
        path = create_review(self.root/'first.json',alias='default',models=['example:Model'],plan=self.plan)
        data, plan = show_review(path)
        self.assertEqual(data['database_alias'], 'default')
        self.assertEqual(plan, self.plan)
        self.assertFalse(data['executable'])

    def test_no_overwrite(self):
        path = self.root/'first.json'
        create_review(path,alias='default',models=[],plan=self.plan)
        with self.assertRaises(FileExistsError):
            create_review(path,alias='default',models=[],plan=self.plan)

    def test_digest_tamper_rejected(self):
        path = create_review(self.root/'first.json',alias='default',models=[],plan=self.plan)
        data = json.loads(path.read_text())
        data['steps'][0]['table'] = 'tampered'
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError,'digest mismatch'):
            show_review(path)

    def test_executable_flag_rejected(self):
        path = create_review(self.root/'first.json',alias='default',models=[],plan=self.plan)
        data = json.loads(path.read_text())
        data['executable'] = True
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            show_review(path)

    def test_invalid_extension_rejected(self):
        with self.assertRaises(ValueError):
            create_review(self.root/'review.sql',alias='default',models=[],plan=self.plan)

    def test_missing_review_rejected(self):
        with self.assertRaises(ValueError):
            show_review(self.root/'missing.json')

    def test_cli_show_review(self):
        path = create_review(self.root/'first.json',alias='analytics',models=[],plan=self.plan)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['db','migration','show',str(path)]),0)
        self.assertIn('Execution disabled.',output.getvalue())
        self.assertIn('apply separately with explicit approval and backup', output.getvalue())
        self.assertIn('analytics',output.getvalue())

    def test_cli_create_requires_config(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(['db','migration','create','--name','sample','--model','example:Model',
                      '--config',str(self.root/'missing.json')])
        self.assertEqual(raised.exception.code,1)

    def test_cli_validate_requires_models(self):
        config = self.root/'aurora_databases.json'
        config.write_text(json.dumps({'databases':{'default':{'url':'sqlite:///missing.sqlite3'}}}))
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(['db','validate','--config',str(config)])
        self.assertEqual(raised.exception.code,1)

    def test_invalid_json_review_rejected(self):
        path = self.root/'invalid.json'
        path.write_text('{broken')
        with self.assertRaises(json.JSONDecodeError):
            show_review(path)
