"""Shared and scoped CLI component compatibility checks."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import create_project, main


class TestScopedComponents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = create_project('site', directory=Path(self.tmp.name), modular=True)

    def invoke(self, *args):
        with patch('pathlib.Path.cwd', return_value=self.root), contextlib.redirect_stdout(io.StringIO()) as output:
            result = main(list(args))
        return result, output.getvalue()

    def test_shared_model_default(self):
        self.invoke('model', 'add', 'User')
        self.assertTrue((self.root / 'models/user.py').is_file())
        self.assertFalse((self.root / 'apps/blog/models/user.py').exists())

    def test_local_model_flag(self):
        self.invoke('model', 'add', 'BlogPost', '--app', 'blog')
        self.assertTrue((self.root / 'apps/blog/models/blog_post.py').is_file())

    def test_legacy_model_positional(self):
        self.invoke('model', 'add', 'blog', 'BlogPost')
        self.assertTrue((self.root / 'apps/blog/models/blog_post.py').is_file())

    def test_shared_view_default(self):
        self.invoke('view', 'add', 'landing')
        self.assertTrue((self.root / 'views/landing.html').is_file())

    def test_local_view_flag(self):
        self.invoke('view', 'add', 'landing', '--app', 'blog')
        self.assertTrue((self.root / 'apps/blog/views/landing.html').is_file())

    def test_shared_list_show(self):
        self.invoke('model', 'add', 'User')
        self.assertIn('user', self.invoke('model', 'list')[1])
        self.assertIn('models', self.invoke('model', 'show', 'user')[1])

    def test_controller_flag(self):
        self.invoke('controller', 'add', 'Article', '--app', 'blog')
        self.assertTrue((self.root / 'apps/blog/controllers/article.py').is_file())

    def test_never_overwrite(self):
        self.invoke('model', 'add', 'User')
        with self.assertRaises(SystemExit):
            self.invoke('model', 'add', 'User')

    def test_production_guard(self):
        with patch.dict(os.environ, {'AURORA_ENV': 'production'}):
            with self.assertRaises(SystemExit):
                self.invoke('model', 'add', 'User')
        self.assertFalse((self.root / 'models/user.py').exists())

    def test_no_database_change(self):
        self.invoke('model', 'add', 'User')
        self.assertFalse(list(self.root.rglob('*.sqlite3')))
