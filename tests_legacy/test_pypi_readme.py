"""Verify the generated package description stays synchronized."""

from pathlib import Path
import subprocess
import sys
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "prepare_pypi_readme.py"


class PyPIReadmeTests(unittest.TestCase):
    def test_generated_readme_is_current(self):
        result = subprocess.run(
            [sys.executable, str(GENERATOR), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_repository_readme_keeps_local_documentation_links(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("](docs/getting-started.md)", readme)
        self.assertIn("](docs/index.md)", readme)

    def test_package_readme_uses_release_tag_links(self):
        readme = (ROOT / "README.pypi.md").read_text(encoding="utf-8")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]
        self.assertIn(
            f"https://github.com/sigmascript/aurora-mvc/blob/v{version}/docs/index.md",
            readme,
        )
        self.assertNotIn("](docs/index.md)", readme)


if __name__ == "__main__":
    unittest.main()
