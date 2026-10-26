"""Version consistency for the Alpha 3 development checkout."""
from pathlib import Path
import re
import tomllib
import aurora
import aurora.core


def test_alpha3_versions_match():
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == "1.0.0a3"
    assert aurora.__version__ == "1.0.0a3"
    assert aurora.core.__version__ == "1.0.0a3"


def test_readme_distinguishes_published_from_development():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "`1.0.0a2` is published" in readme
    assert "`1.0.0a3` is under development" in readme
