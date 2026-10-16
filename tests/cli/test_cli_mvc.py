"""MVC scaffolding contracts."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import create_project, main


class TestMVCScaffold(unittest.TestCase):
    def test_mvc_files_and_concrete_controller(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('sample_mvc', directory=Path(temp), mvc=True)
            self.assertTrue((root / 'controllers' / 'home.py').is_file())
            self.assertTrue((root / 'views' / 'home.html').is_file())
            self.assertIn('class Home(Controller)', (root / 'controllers' / 'home.py').read_text())
            self.assertIn('app.route("/", Home)', (root / 'app.py').read_text())
            self.assertFalse((root / 'services').exists())

    def test_mvc_app_imports_from_own_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('sample_mvc', directory=Path(temp), mvc=True)
            env = dict(os.environ)
            env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), env.get('PYTHONPATH')]))
            result = subprocess.run([sys.executable, '-c', 'from app import app; assert app.renderer is not None; assert app.router is not None'], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_new_mvc(self):
        with tempfile.TemporaryDirectory() as temp, patch('aurora.cli.Path.cwd', return_value=Path(temp)):
            self.assertEqual(main(['new', 'sample_mvc', '--mvc']), 0)
            self.assertTrue((Path(temp) / 'sample_mvc' / 'controllers' / 'home.py').exists())

    def test_mvc_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            create_project('sample_mvc', directory=Path(temp), mvc=True)
            with self.assertRaises(FileExistsError):
                create_project('sample_mvc', directory=Path(temp), mvc=True)
