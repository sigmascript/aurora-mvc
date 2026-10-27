"""Canonical resource-first CLI behavior, without changing existing handlers."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import create_project, main


class GroupedCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = create_project('sample', directory=Path(self.temp.name), modular=True)

    def invoke(self, *args):
        output = io.StringIO()
        with patch('pathlib.Path.cwd', return_value=self.root), contextlib.redirect_stdout(output):
            self.assertEqual(main(list(args)), 0)
        return output.getvalue()

    def test_app_list_and_show(self):
        self.assertIn('blog', self.invoke('app', 'list'))
        self.assertIn('App: blog', self.invoke('app', 'show', 'blog'))

    def test_app_add_and_legacy_alias(self):
        self.invoke('app', 'add', 'news')
        self.assertTrue((self.root / 'apps/news/__init__.py').is_file())
        self.invoke('create-app', 'events')
        self.assertIn('events', self.invoke('app', 'list'))

    def test_controller_and_view_add(self):
        self.invoke('controller', 'add', 'blog', 'News')
        self.invoke('view', 'add', 'blog', 'news')
        self.assertTrue((self.root / 'apps/blog/controllers/News.py').is_file())
        self.assertTrue((self.root / 'apps/blog/views/news.html').is_file())
        self.assertIn('News', (self.root / 'app.py').read_text())

    def test_add_never_overwrites(self):
        self.invoke('view', 'add', 'blog', 'news')
        file = self.root / 'apps/blog/views/news.html'
        file.write_text('developer content')
        with patch('pathlib.Path.cwd', return_value=self.root), self.assertRaises(SystemExit) as raised:
            main(['view', 'add', 'blog', 'news'])
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(file.read_text(), 'developer content')

    def test_production_guard(self):
        with patch.dict(os.environ, {'AURORA_ENV': 'production'}), self.assertRaises(SystemExit) as raised:
            main(['app', 'add', 'news'])
        self.assertEqual(raised.exception.code, 1)

    def test_unimplemented_actions_not_advertised(self):
        for argv in (['app', 'remove', 'blog'], ['db', 'migrate']):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(argv)
            self.assertEqual(raised.exception.code, 2)

    def test_group_help(self):
        for argv in (['app', '--help'], ['controller', '--help'], ['view', '--help']):
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(argv)
            self.assertEqual(raised.exception.code, 0)


if __name__ == '__main__':
    unittest.main()
