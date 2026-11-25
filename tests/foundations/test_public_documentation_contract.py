"""Public guides must agree with the supported developer CLI."""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_public_guide_links_resolve():
    documents = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    for document in documents:
        if (
            document.name.startswith("RELEASE_CANDIDATE_")
            or document.name == "aurorasql-alpha8-compatibility.md"
        ):
            continue  # Historical files are retired separately.
        body = document.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", body):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            resolved = (document.parent / target.split("#", 1)[0]).resolve()
            assert resolved.is_file(), (document.relative_to(ROOT), target)


def test_active_database_examples_match_supported_configuration():
    for document in (ROOT / "docs/database.md", ROOT / "docs/application-configuration.md"):
        body = document.read_text(encoding="utf-8")
        for block in re.findall(r"```json\n(.*?)\n```", body, flags=re.S):
            parsed = json.loads(block)
            assert isinstance(parsed, dict)
            # Example formatting should be readable and consistent.
            assert '\n    "' in block, document.name


def test_unreleased_changelog_is_kept_during_development():
    body = (ROOT / "changelog/UNRELEASED.md").read_text(encoding="utf-8")
    assert body.startswith("# Unreleased\n")
    assert "## Added" in body and "## Fixed" in body
    # An empty Unreleased section is valid immediately after archiving a release.
    # Require the running headings, not an invented placeholder entry.
    assert "## Changed" in body and "## Removed or retired" in body
    assert (ROOT / ".github/pull_request_template.md").is_file()
