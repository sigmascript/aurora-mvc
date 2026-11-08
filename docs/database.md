# AuroraSQL database operations and safety

This guide describes the integrated **AuroraSQL** backend in the `1.0.0a7` source candidate. The former SQLAlchemy-backed `aurora.database` implementation is retired. AuroraSQL is alpha software, and a command's existence does **not** imply support for every database engine or schema operation. The SQLite explicit JSON workflow below is **not** part of the older published `1.0.0a6` release; `1.0.0a7` publication awaits approval.

## Configuration and drivers

Database aliases can be configured in `aurora_databases.json`. Project models are discovered automatically from shared and application model directories in the normal, MVC and modular layouts; `aurora_models.json` is an optional explicit mapping. Where supported, `url_env` reads the connection URL from an environment variable. Never commit database credentials. SQLite is included; MySQL and PostgreSQL require optional drivers (`PyMySQL` and `psycopg`).

A normal Aurora project can use just one `default` database. Model directory location is independent of database alias; advanced multi-database configurations remain optional. Every reviewed migration selects exactly one alias.

## Public CLI

| Command | Purpose | Important limit |
| --- | --- | --- |
| `init` | Initialize discovered/registered model tables | Only supported empty-schema initialization; never use to migrate an existing schema |
| `plan` | Inspect selected schema and report differences | Read-only; inspection is not approval to write |
| `migrate` | Preview/apply selected supported schema changes | Backend-specific scope, review, backup, and explicit approval gates |
| `backup` | Create a verified backup | SQLite and guarded native MySQL/PostgreSQL paths; backend tools and client prerequisites vary |
| `restore` | Reserved recovery command | **Execution disabled**; refuses unsafe legacy fallback |
| `history` | Read migration records | Does not itself verify the entire database schema |

There are exactly six public database commands.

## Ordinary model workflow

```sh
aurora db init
aurora db plan
aurora db plan --check
aurora db migrate
aurora db history
```

The first `init` command is appropriate only when its target is eligible for initialization. Ordinary interactive migrations review proposed changes and ask for confirmation; non-interactive automation has separate explicit approval prerequisites. Add `--database accounting` for a configured alias or `--title "..."` to name a migration where supported. Model discovery does not require a `--model MODULE:CLASS` argument. Server migrations support a narrowly reviewed single change at a time, including repeated guarded successors; history must be independently proved before every later change. **Migration3 was verified on disposable MySQL and PostgreSQL databases** with existing-row preservation, but no production recovery or universal migration parity is certified. Do **not** invent hashes or bypass history checks.

## Explicit schema operations: `aurora db migrate -c`

**Purpose:** Describe an intentional, single-table rename in a small JSON file rather than guessing whether a removed model field was renamed. The new `-c` flag is short for `--changes`, not the existing `--config` database-connection flag.

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

### Review first, then execute if supported

```sh
# Read only: inspect the selected schema and obtain its exact review fingerprint.
aurora db migrate -c renames.json --dry-run --format json

# Optional automation check: exit 2 for supported pending change, 3 for refusal.
aurora db migrate -c renames.json --dry-run --check

# Example guarded SQLite execution only, after human review:
# Replace REVIEW_SHA256 with the exact fingerprint from the first command.
# The backup filename must not exist; its parent directory must already exist.
aurora db migrate -c renames.json \
  --apply --confirm APPLY --approve REVIEW_SHA256 \
  --backup backups/before_rename.sqlite3
```

These commands are **not an instruction to run DDL on important or production data**. Examine the review and test a copy of the database first. The SQLite executor requires an existing, compatible `aurorasql_migration_intents_v2` journal; it never initializes journals implicitly. It performs a bounded in-memory same-engine rehearsal (maximum source database size 64 MiB), creates and verifies a new exclusive backup, rechecks the exact schema and review fingerprint under a write lock, then executes SQLite rename DDL and the intent journal in one transaction. It checks the final catalog, integrity, foreign keys and preserved row count.

The supported rename subset intentionally refuses tables with unresolved migration history, recorded historical table operations requiring reconciliation, foreign-key dependencies, views, triggers, target indexes or advanced table constructs. A refusal is a safety boundary, not permission to bypass it with manual flags. The backup is preserved after a failure. The operation file must be updated alongside the Python model declaration when the real table or field name changes.

The **MySQL and PostgreSQL explicit-operation paths currently refuse execution**, and the preview reports backend inspection as unimplemented. MySQL DDL cannot be treated as universally rollbackable. Complex constraint changes, destructive drops, cross-database migrations, and PK changes remain unsupported here. The broader Milestone 4 model-change, configuration, and recovery work is not completed by this initial rename feature.

## Current limits and recovery

`aurora db restore` refuses execution. Recovery preflight or staging helper modules do not constitute an enabled, operationally verified restoration workflow. Maintain an independently tested recovery procedure before relying on AuroraSQL for important data.

### Recovery rehearsal design (not an enabled restore feature)

For a future **separately authorized disposable** same-engine recovery rehearsal, the operator must first record the database engine/version, exact source identity and alias, migration timeline state, backup path and integrity/catalog evidence. Select a newly provisioned isolated target with independently verified ownership, privileges and identity; never point recovery at the active application database. Verify the backup was created for the same engine, retain its original bytes, and arrange a separate protected copy for any restore tool. Before permitting any write, require explicit approval naming the disposable target and exact procedure. A read-only preflight is not that approval.

The rehearsal must then compare restored schema, row evidence, migration timeline and operation journal against the recorded source evidence; deliberately exercise a corrupted/incomplete backup refusal, changed-target-identity refusal and uncertain-journal refusal. Any mismatch stops the rehearsal for manual reconciliation without retry or journal repair. Preserve original source and backup evidence, independently confirm that the active database was not modified, and record the observed outcome. **No such live restoration has been authorized or demonstrated.** This design does not enable `aurora db restore`, cross-engine conversion, automated disaster recovery or production certification.

Live backend tests require explicitly disposable databases and specific authorization. Verify connection identity and table ownership before DDL. A successful local regression is not proof of current CI, other database engines, production concurrency or complete recovery readiness. After uncertain MySQL DDL, preserve schema and journal evidence and **never blindly retry**.
