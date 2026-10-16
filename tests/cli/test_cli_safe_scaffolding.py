"""Safe MVC scaffolding and registration regression tests."""
import ast
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from jinja2 import Environment
from aurora.cli import create_project, create_controller, create_view, main


class SafeScaffoldingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = create_project('sample', directory=Path(self.temp.name), modular=True)

    def test_controller_registers_import_and_route(self):
        path = create_controller('blog', 'News', directory=self.root)
        content = path.read_text(encoding='utf-8')
        self.assertNotIn(r'\n', content)
        ast.parse(content)
        app_source = (self.root / 'app.py').read_text(encoding='utf-8')
        self.assertIn('from apps.blog.controllers.news import News', app_source)
        self.assertIn('app.route("/blog/news", News)', app_source)
        ast.parse(app_source)

    def test_view_uses_existing_layout(self):
        path = create_view('blog', 'news', directory=self.root)
        content = path.read_text(encoding='utf-8')
        self.assertNotIn(r'\n', content)
        Environment().parse(content)
        self.assertIn('{% extends "layout.html" %}', content)

    def test_never_overwrites_or_double_registers(self):
        path = create_controller('home', 'Profile', directory=self.root)
        before = (self.root / 'app.py').read_bytes()
        path.write_text('user changes')
        with self.assertRaises(FileExistsError):
            create_controller('home', 'Profile', directory=self.root)
        self.assertEqual(path.read_text(), 'user changes')
        self.assertEqual(before, (self.root / 'app.py').read_bytes())
        create_view('home', 'profile', directory=self.root)
        with self.assertRaises(FileExistsError):
            create_view('home', 'profile', directory=self.root)

    def test_rejects_missing_anchors_without_creating_controller(self):
        app = self.root / 'app.py'
        app.write_text(app.read_text().replace('# aurora:routes', '# custom routes'))
        with self.assertRaises(ValueError):
            create_controller('home', 'Settings', directory=self.root)
        self.assertFalse((self.root / 'apps/home/controllers/settings.py').exists())

    def test_rejects_invalid_names_and_missing_apps(self):
        for name in ('../evil', 'bad/name', 'lowercase', 'A-B'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                create_controller('home', name, directory=self.root)
        for name in ('../evil', 'x/y', 'UPPER'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                create_view('home', name, directory=self.root)
        with self.assertRaises(ValueError):
            create_view('missing', 'index', directory=self.root)

    def test_cli_production_guard(self):
        with patch.dict(os.environ, {'AURORA_ENV': 'production'}):
            with self.assertRaises(SystemExit) as caught:
                main(['create-controller', 'home', 'Sample'])
        self.assertEqual(caught.exception.code, 1)

    def test_rejects_symlink_app(self):
        apps = self.root / 'apps'
        try:
            (apps / 'alias').symlink_to(apps / 'home', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError):
            create_view('alias', 'sample', directory=self.root)


if __name__ == '__main__':
    unittest.main()
