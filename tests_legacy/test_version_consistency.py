"""Version consistency and release documentation checks."""

from pathlib import Path
import subprocess
import sys
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import aurora
import aurora.core


class VersionConsistencyTests(unittest.TestCase):
    """Verify package versions and release documentation."""

    def test_package_versions_match(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )

        expected_version = project["project"]["version"]

        self.assertEqual(aurora.__version__, expected_version)
        self.assertEqual(aurora.core.__version__, expected_version)

    def test_readme_identifies_checkout_version(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")

        project = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertIn(f"`{project['project']['version']}`", readme)

    def test_release_documentation_identifies_candidate(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        version = project["project"]["version"]

        for relative_path in ("docs/index.md", "docs/getting-started.md", "docs/cli.md"):
            with self.subTest(document=relative_path):
                content = (root / relative_path).read_text(encoding="utf-8")
                self.assertIn(version, content)

    def test_migration_help_describes_supported_model_changes(self):
        result = subprocess.run(
            [sys.executable, "-m", "aurora", "db", "migrate", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Update your database to match supported model changes", result.stdout)
        self.assertIn("JSON changes file", result.stdout)
        self.assertIn("asks for confirmation", result.stdout)
        self.assertIn("Unsupported or unsafe changes are refused", result.stdout)


if __name__ == "__main__":
    unittest.main()
