"""Exercise historical guarded migration adapters without reopening retired CLI flags.

The public CLI no longer accepts execution digests and backend internals.
These tests keep verifying historical database safety from the internal boundary.
"""

import argparse

from aurora.cli import main as public_main


_LEGACY_OPTIONS = frozenset({
    "--dry-run", "--format", "--check", "--apply", "--approve", "--backup",
    "--review-file", "--name", "--output-dir", "--schema", "--table",
    "--column", "--approve-review", "--approve-sql", "--model", "--explain",
})


def legacy_migration_or_public_cli(arguments):
    """Route retired-option test cases to the same guarded internal executors."""
    if arguments[:2] != ["db", "migrate"] or not any(
        option.split("=", 1)[0] in _LEGACY_OPTIONS for option in arguments[2:]
    ):
        return public_main(arguments)

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--change", dest="changes")
    parser.add_argument("--database", default="default")
    parser.add_argument("--title")
    parser.add_argument("--confirm")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approve")
    parser.add_argument("--backup")
    parser.add_argument("--review-file")
    parser.add_argument("--name")
    parser.add_argument("--output-dir", default="migrations")
    parser.add_argument("--schema")
    parser.add_argument("--table")
    parser.add_argument("--column")
    parser.add_argument("--approve-review")
    parser.add_argument("--approve-sql")
    parser.add_argument("--model", action="append", default=[])
    args = parser.parse_args(arguments[2:])
    args.config = "aurora_databases.json"
    args.models_config = "aurora_models.json"
    args.login_path = None

    try:
        if args.dry_run and args.backup is not None:
            raise PermissionError("Read-only preview does not accept a backup path")

        if args.changes:
            from aurora.database.cli_explicit_operations import run_explicit_operations
            return run_explicit_operations(args)

        if args.review_file:
            from aurora.database.cli_migrate_reviewed import run_reviewed_migration
            return run_reviewed_migration(args)

        from aurora.database.cli_model_migration import run_model_migration

        try:
            return run_model_migration(args)
        except (RuntimeError, ValueError):
            if not args.dry_run:
                raise

            from aurora.database.cli_plan import run_plan
            return run_plan(args)
    except Exception as error:
        # The public adapter historically reports execution refusal with exit 1.
        raise SystemExit(1) from error
