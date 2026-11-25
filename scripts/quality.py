"""Read-only, repeatable Aurora Python source-quality assessment.

Use ``python scripts/quality.py audit`` during migration toward clean formatting,
then ``python scripts/quality.py check`` as a strict gate after acceptance.
Neither command formats, fixes imports, or changes repository files.
"""

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCOPE = ("aurora", "tests", "scripts")


def run_ruff(*arguments):
    """Run the pinned development tool in this project without editing source."""
    return subprocess.run(
        [sys.executable, "-m", "ruff", *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _source_label(filename):
    """Display a diagnostic path relative to the repository when possible."""
    path = Path(filename or "unknown")
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def summarize_lint(issues):
    """Show concentrated violations with distinct, actionable source paths."""
    by_rule = Counter(item.get("code") or "unknown" for item in issues)
    by_file = Counter(_source_label(item.get("filename")) for item in issues)

    print(f"Lint findings: {len(issues)}")
    if by_rule:
        print("Most frequent rules:")
        for code, count in by_rule.most_common(12):
            print(f"  {code}: {count}")

        print("Most affected source paths:")
        for name, count in by_file.most_common(12):
            print(f"  {name}: {count}")

    # These need source review: repeated imports may be intentional in distinct
    # execution paths, while E402 can reflect deliberately delayed setup.
    # Keep this diagnostic short; do not rewrite or re-home imports here.
    for code, title in (
        ("F811", "Repeated bindings (F811)"),
        ("E402", "Imports after statements (E402)"),
    ):
        matches = [item for item in issues if item.get("code") == code]
        if not matches:
            continue
        print(f"{title} — sample locations ({min(len(matches), 8)}/{len(matches)}):")
        for item in matches[:8]:
            position = item.get("location") or {}
            location = f"{_source_label(item.get('filename'))}:{position.get('row', '?')}"
            print(f"  {location}: {item.get('message') or code}")

    # Undefined names are potential runtime problems, not mere formatting debt.
    # Report them explicitly rather than burying them among import-order findings.
    unresolved = [item for item in issues if item.get("code") == "F821"]
    if unresolved:
        print("Undefined names (F821) — inspect manually before attempting fixes:")
        for item in unresolved:
            position = item.get("location") or {}
            location = f"{_source_label(item.get('filename'))}:{position.get('row', '?')}"
            print(f"  {location}: {item.get('message') or 'undefined name'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("audit", "check"),
        help="audit reports existing findings; check fails if any rule is unmet",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=None,
        help="optional repository-relative paths; default: aurora tests scripts",
    )
    args = parser.parse_args()
    paths = args.paths or DEFAULT_SCOPE

    for raw_path in paths:
        candidate = (PROJECT_ROOT / raw_path).resolve()
        if not candidate.is_relative_to(PROJECT_ROOT) or not candidate.exists():
            print(f"Invalid or outside-project path: {raw_path}", file=sys.stderr)
            return 2

    version = run_ruff("--version")
    if version.returncode != 0:
        print("Ruff unavailable in this Python environment.", file=sys.stderr)
        print(version.stderr.strip(), file=sys.stderr)
        return 2
    print(f"Aurora source-quality {args.mode}: {version.stdout.strip()}")
    print("Scope:", ", ".join(paths))

    format_result = run_ruff("format", "--check", "--quiet", *paths)
    if format_result.returncode not in (0, 1):
        print("Ruff formatting check could not run:", file=sys.stderr)
        print(format_result.stderr.strip(), file=sys.stderr)
        return 2

    print("\n=== Formatting ===")
    if format_result.returncode == 0:
        print("PASS: all inspected files match the Ruff formatter.")
    else:
        print("NOT YET CLEAN: some files require Ruff formatting.")
        print("Use `python -m ruff format --check <path>` to inspect specific files.")

    lint_result = run_ruff("check", "--output-format", "json", *paths)
    if lint_result.returncode not in (0, 1):
        print("Ruff lint check could not run:", file=sys.stderr)
        print(lint_result.stderr.strip(), file=sys.stderr)
        return 2
    try:
        issues = json.loads(lint_result.stdout)
        if not isinstance(issues, list):
            raise ValueError("expected JSON issue list")
    except (ValueError, TypeError) as exc:
        print(f"Could not parse Ruff diagnostics: {exc}", file=sys.stderr)
        return 2

    print("\n=== Lint ===")
    summarize_lint(issues)
    print("\nNo files were modified by this quality workflow.")

    if args.mode == "check" and (format_result.returncode or lint_result.returncode):
        print("QUALITY CHECK FAILED: review and fix findings before enforcing in CI.")
        return 1
    if args.mode == "audit" and (format_result.returncode or lint_result.returncode):
        print("AUDIT COMPLETE: existing findings are recorded, not auto-fixed.")
    else:
        print("QUALITY CHECK PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
