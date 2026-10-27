"""Version consistency checks for the Alpha 3 development checkout."""

from pathlib import Path
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import aurora
import aurora.core


class Alpha3VersionConsistencyTests(unittest.TestCase):
    """Verify package versions and development release documentation."""

    def test_alpha3_versions_match(self):
        root = Path(__file__).resolve().parents[1]
        project = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(project["project"]["version"], "1.0.0a3")
        self.assertEqual(aurora.__version__, "1.0.0a3")
        self.assertEqual(aurora.core.__version__, "1.0.0a3")

    def test_readme_distinguishes_published_from_development(self):
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertIn("`1.0.0a2` is published", readme)
        self.assertIn("`1.0.0a3` is under development", readme)


if __name__ == "__main__":
    unittest.main()
