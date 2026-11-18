# Unreleased

These are changes in the development checkout. They are **not** part of the published `1.0.0a7` package. Check each item again before the next release.

## Added

- Choose a configured database when generating a model with `aurora model add NAME --database DATABASE`.
- Inspect verified backup records with `aurora db history --backups`.
- Review supported forward-only SQLite/MySQL/PostgreSQL migrations, including guarded foreign keys, unique constraints, indexes, explicit renames, and intentional table/column removal where eligible.
- Keep database snapshots and backups under named `__migrations__/` and `__backups__/` directories.

## Changed

- Use named database configuration and a single project `default_database` selection; generated MVC projects start with a `data` SQLite database.
- Keep migration backups optional. Destructive operations still require review, physical-state checks, and explicit confirmation.
- Give supported server migrations one coherent review for eligible combinations of ordinary column changes.
- Store forward-history and backup-catalog evidence using the selected database identity; `restore` remains disabled.
- Refresh the public guides around current commands, real configuration examples, and clear distinctions between published and unreleased behavior.

## Fixed

- Build release artifacts with the declared setuptools version, keeping the existing SPDX license metadata.
- Keep the published-version banner in the root README and its generated PyPI copy, without repeating it in individual developer guides.

- Make the test runner’s async fixture scope explicit and verify release-package boundaries.

- Reject duplicate model registrations before creating a model file.
- Make SQLite's older index-removal review reject missing, unrelated, or constraint-owned indexes safely.
- Distinguish MySQL pre-DDL failures from uncertain outcomes after DDL begins in guarded table removal.

## Removed or retired

- Retired the historical `tests_legacy/` tree after moving essential authentication-guard checks into the active pytest suite.
- Retired public migration options and automatic migration-time backups; standalone `aurora db backup` remains available.

## Before release

- Verify each bullet against the final source, public CLI, regression results, and real-app acceptance. Record any user-facing compatibility changes clearly.
