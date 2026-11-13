# AuroraSQL database operations and safety

This guide describes the integrated **AuroraSQL** backend in the **unreleased Milestone 5 development checkout**, which extends the published `1.0.0a7` alpha. The former SQLAlchemy-backed `aurora.database` implementation is retired. AuroraSQL is alpha software; a command's existence does **not** imply support for every database engine or schema operation. Checkouts and published packages have different capability boundaries.

## Configuration and drivers

Database aliases can be configured in `aurora_databases.json`. Project models are discovered automatically from shared and application model directories in the normal, MVC and modular layouts; `aurora_models.json` is an optional explicit mapping. Where supported, `url_env` reads the connection URL from an environment variable. Never commit database credentials. SQLite is included; MySQL and PostgreSQL require optional drivers (`PyMySQL` and `psycopg`).

A normal Aurora project can use just one `default` database. Model directory location is independent of database alias; advanced multi-database configurations remain optional. Every reviewed migration selects exactly one alias.

## Public CLI

| Command | Purpose | Important limit |
| --- | --- | --- |
| `init` | Initialize discovered model tables | Only supported empty-schema initialization; never use to migrate an existing schema |
| `check` | Inspect database readiness and model differences | Read-only; inspection is not approval to write |
| `migrate` | Preview/apply selected supported schema changes | Backend-specific scope, review, and explicit approval gates; backups optional |
| `backup` | Create a verified backup | SQLite and guarded native MySQL/PostgreSQL paths; backend tools and client prerequisites vary |
| `restore` | Guarded SQLite restore into an absent file | Server restoration disabled |
| `history` | Read migration records | Does not itself verify the entire database schema |

There are exactly six public database commands.

## Ordinary model workflow

```sh
aurora db init
aurora db check
aurora db migrate
aurora db history
```

The first `init` command is appropriate only when its target is eligible for initialization. Ordinary interactive migrations review proposed changes and ask for confirmation. Supported non-destructive changes may use `--confirm yes`; populated SQLite table/column deletion additionally requires interactive exact-target confirmation. Add `--database accounting` for a configured alias or `--title "..."` to name a migration where supported. Model discovery does not require a `--model MODULE:CLASS` argument. Server migrations support a narrowly reviewed single change at a time, including repeated guarded successors; history must be independently proved before every later change. **Migration3 was verified on disposable MySQL and PostgreSQL databases** with existing-row preservation, but no production recovery or universal migration parity is certified. Do **not** invent hashes or bypass history checks.

## Explicit schema operations: `aurora db migrate -c`

**Purpose:** Describe an intentional, single-table rename in a small JSON file rather than guessing whether a removed model field was renamed. The `-c` flag is short for `--change`.

For two column renames on `users`:

```json
{
  "database": "default",
  "rename_columns": {
    "users": {
      "user": "user_name",
      "visist": "visits"
    }
  }
}
```

For one table rename:

```json
{
  "database": "default",
  "rename_table": {
    "message": "messages"
  }
}
```

One file selects **one operation type and one table**; it can rename several independent columns within that one table. The optional `format` field defaults to `aurorasql.operations.v1` for backward compatibility. Renames are never inferred automatically. Unknown JSON keys, invalid SQL identifiers, duplicate or chained renames, wrong aliases, and unrelated operations are rejected. The operation file does not contain executable SQL.

### Current public confirmation boundary

`aurora db migrate -c renames.json` selects one explicit JSON operation document. Public migration flags are deliberately minimal; the older `--dry-run`, `--apply`, `--approve`, `--backup` and review-digest arguments are not available. Do not infer broad rename or destructive-operation support from a JSON document. Unsupported operations fail closed, and MySQL/PostgreSQL explicit JSON execution is refused.


### Guarded model-driven deletion (unreleased checkout only)

For a database with verified **forward-only SQLite history**, removing exactly one existing table or one column from the Python model can be reviewed through ordinary `aurora db check` and `aurora db migrate`. Aurora refuses unsupported dependencies, mixed changes, inconsistent history/snapshots, and changed data after review. Empty tables and columns with no non-NULL values require ordinary migration confirmation; populated tables and columns with existing non-NULL values additionally require typing the **exact table** or **`table.column`** identifier interactively. `--confirm yes` never bypasses that second confirmation. Execution rechecks reviewed data under SQLite's write lock and records approved potential data-loss warnings in `migrations.warnings`.

Do **not** interpret this as arbitrary destructive DDL support. Lossy narrowing or constraint-incompatible changes remain refused; MySQL/PostgreSQL table and column deletion is not enabled. Use a disposable database for experimentation, never an important existing database.

## Current limits and recovery

`aurora db restore` supports only bounded SQLite restoration into an absent file with matching verified snapshots. MySQL/PostgreSQL restoration remains disabled. Preflight and staging helpers do not certify server restoration. Maintain an independently tested recovery procedure before relying on AuroraSQL for important data.

### Recovery rehearsal design (not an enabled restore feature)

For a future **separately authorized disposable** same-engine recovery rehearsal, the operator must first record the database engine/version, exact source identity and alias, migration timeline state, backup path and integrity/catalog evidence. Select a newly provisioned isolated target with independently verified ownership, privileges and identity; never point recovery at the active application database. Verify the backup was created for the same engine, retain its original bytes, and arrange a separate protected copy for any restore tool. Before permitting any write, require explicit approval naming the disposable target and exact procedure. A read-only preflight is not that approval.

The rehearsal must then compare restored schema, row evidence, migration timeline and operation journal against the recorded source evidence; deliberately exercise a corrupted/incomplete backup refusal, changed-target-identity refusal and uncertain-journal refusal. Any mismatch stops the rehearsal for manual reconciliation without retry or journal repair. Preserve original source and backup evidence, independently confirm that the active database was not modified, and record the observed outcome. **No such live restoration has been authorized or demonstrated.** This design does not enable `aurora db restore`, cross-engine conversion, automated disaster recovery or production certification.

Live backend tests require explicitly disposable databases and specific authorization. Verify connection identity and table ownership before DDL. A successful local regression is not proof of current CI, other database engines, production concurrency or complete recovery readiness. After uncertain MySQL DDL, preserve schema and journal evidence and **never blindly retry**.
