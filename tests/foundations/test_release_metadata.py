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


def test_published_version_notice_stays_in_root_readme():
    version = _version()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    pypi_readme = (ROOT / "README.pypi.md").read_text(encoding="utf-8")
    assert "**Latest published alpha:** [`1.0.0a7`]" in readme
    assert "**Latest published alpha:** [`1.0.0a7`]" in pypi_readme
    assert f"**Release candidate in preparation:** `{version}`" in readme
    assert f"**Release candidate in preparation:** `{version}`" in pypi_readme
    for relative in (
        "docs/index.md", "docs/getting-started.md",
        "docs/cli.md", "docs/database.md",
    ):
        content = (ROOT / relative).read_text(encoding="utf-8")
        assert "Latest published alpha:" not in content, relative
        assert version not in content, relative


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
    prefix = f"https://github.com/sigmascript/aurora-mvc/blob/v{_version()}/"
    assert pypi.replace(prefix + "docs/", "docs/") == repo


def test_distribution_contains_only_aurora_python_packages():
    """Keep tests, private records, and development helpers out of the wheel."""
    from setuptools import find_packages

    package_roots = set(find_packages(where=str(ROOT), include=["aurora*"]))
    assert package_roots == {
        "aurora", "aurora.core", "aurora.database", "aurora.security",
    }

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["tool"]["setuptools"]["packages"]["find"]["include"] == ["aurora*"]
    assert metadata["project"]["readme"]["file"] == "README.pypi.md"


def test_pytest_fixture_scope_is_explicit():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert metadata["tool"]["pytest"]["ini_options"]["asyncio_default_fixture_loop_scope"] == "function"
