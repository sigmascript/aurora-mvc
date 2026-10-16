"""Alpha distribution metadata and public API consistency checks."""

import pathlib
import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import aurora
import aurora.core


ROOT = pathlib.Path(__file__).resolve().parents[2]


class PackageDistributionContractTests(unittest.TestCase):
    def test_distribution_identity(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["name"], "aurora-mvc")
        self.assertEqual(metadata["project"]["version"], "1.0.0a1")

    def test_import_versions_match_distribution(self):
        self.assertEqual(aurora.__version__, "1.0.0a1")
        self.assertEqual(aurora.core.__version__, aurora.__version__)

    def test_cli_entry_point_is_preserved(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["scripts"]["aurora"], "aurora.cli:main")

    def test_public_repository_metadata(self):
        metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(metadata["project"]["urls"]["Repository"],
                         "https://github.com/sigmascript/aurora-mvc")

    def test_cli_install_hint_uses_new_distribution(self):
        cli = (ROOT / "aurora" / "cli.py").read_text(encoding="utf-8")
        self.assertIn("pip install 'aurora-mvc[dev]'", cli)
        self.assertNotIn("pip install 'auroramvc[dev]'", cli)


if __name__ == "__main__":
    unittest.main()
