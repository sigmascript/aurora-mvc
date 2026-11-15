"""Repository release checks; not shipped as runtime dependencies."""

from pathlib import Path
import subprocess
import sys

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import aurora
import aurora.core


ROOT = Path(__file__).resolve().parents[2]


def _version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_package_versions_match():
    assert aurora.__version__ == aurora.core.__version__ == _version()


def test_release_documentation_identifies_version():
    version = _version()
    assert f"`{version}`" in (ROOT / "README.md").read_text(encoding="utf-8")
    for relative in ("docs/index.md", "docs/getting-started.md", "docs/cli.md"):
        assert version in (ROOT / relative).read_text(encoding="utf-8"), relative


def test_generated_pypi_readme_matches_source():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "prepare_pypi_readme.py"), "--check"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pypi_readme_links_are_version_pinned():
    repo = (ROOT / "README.md").read_text(encoding="utf-8")
    pypi = (ROOT / "README.pypi.md").read_text(encoding="utf-8")
    assert "](docs/index.md)" in repo
    assert "](docs/getting-started.md)" in repo
    assert f"https://github.com/sigmascript/aurora-mvc/blob/v{_version()}/docs/index.md" in pypi
    assert "](docs/index.md)" not in pypi
