"""Generate and verify PyPI README metadata from the repository README."""

import argparse
from pathlib import Path
import re
import sys

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "README.md"
TARGET = ROOT / "README.pypi.md"
PROJECT = ROOT / "pyproject.toml"
REPOSITORY = "https://github.com/sigmascript/aurora-mvc"
DOC_LINK = re.compile(r"(?P<open>\]\()(?P<path>docs/[A-Za-z0-9_./-]+)(?P<close>\))")


def generate():
    metadata = tomllib.loads(PROJECT.read_text(encoding="utf-8"))
    version = metadata["project"]["version"]
    source = SOURCE.read_text(encoding="utf-8")
    if f"`{version}`" not in source:
        raise ValueError(f"README.md does not identify project version {version}")

    paths = set(match.group("path") for match in DOC_LINK.finditer(source))
    if not paths:
        raise ValueError("README.md has no relative documentation links")
    for path in paths:
        resolved = (ROOT / path).resolve()
        if not resolved.is_relative_to(ROOT.resolve()) or not resolved.is_file():
            raise ValueError(f"Missing or unsafe documentation path: {path}")

    prefix = f"{REPOSITORY}/blob/v{version}/"
    return DOC_LINK.sub(lambda match: f"]({prefix}{match.group('path')})", source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check without modifying files")
    args = parser.parse_args()
    try:
        expected = generate()
        if args.check:
            if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != expected:
                print("FAIL: README.pypi.md is missing or out of sync. Run the generator.", file=sys.stderr)
                return 1
            print("PASS: README.pypi.md matches README.md and pyproject.toml")
            return 0
        TARGET.write_text(expected, encoding="utf-8", newline="")
        print(f"Generated {TARGET.relative_to(ROOT)}")
        return 0
    except (OSError, KeyError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
