"""Phase 5.3: optional modular MVC starter."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from aurora.cli import create_project, main


class TestModularScaffold(unittest.TestCase):
    def test_modular_layout_has_concrete_controller_and_view(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project("sample_modular", directory=Path(temp), modular=True)
            self.assertTrue((root / "apps/home/controllers/home.py").is_file())
            self.assertTrue((root / "apps/home/views/index.html").is_file())
            self.assertFalse((root / "controllers").exists())
            self.assertFalse((root / "services").exists())
            self.assertIn('app.route("/", Home)', (root / "app.py").read_text())

    def test_modular_application_imports(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project("sample_modular", directory=Path(temp), modular=True)
            env = dict(os.environ)
            env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(Path(__file__).resolve().parents[2]), env.get("PYTHONPATH")]))
            result = subprocess.run([sys.executable, "-c", "from app import app; assert app.renderer is not None"], cwd=root, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_modular_option(self):
        with tempfile.TemporaryDirectory() as temp, patch("aurora.cli.Path.cwd", return_value=Path(temp)):
            self.assertEqual(main(["new", "sample_modular", "--modular"]), 0)
            self.assertTrue((Path(temp) / "sample_modular/apps/home/controllers/home.py").exists())

    def test_modular_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            create_project("sample_modular", directory=Path(temp), modular=True)
            with self.assertRaises(FileExistsError):
                create_project("sample_modular", directory=Path(temp), modular=True)
