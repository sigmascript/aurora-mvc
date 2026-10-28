"""Version consistency and release documentation checks."""

from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()