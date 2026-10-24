# Database and migration safety

Aurora MVC provides SQLAlchemy-based database interfaces and CLI tools for configuring named databases, registering models, inspecting existing SQLite schemas, and reviewing limited schema migrations. Database operations require more care than the minimal web starter.

## Inspect commands first

Run these commands from the root of the **intended project**:

```bash
aurora db --help
aurora db migration --help
aurora db sync --help
```

The `db` command groups are:

| Command | Purpose |
| --- | --- |
| `init` | Initialize an empty SQLite or MySQL database from explicitly selected models; refuse partial or unrelated schemas |
| `list`, `show` | Inspect configured database aliases and their details |
| `check`, `inspect` | Check or inspect an existing SQLite or MySQL database |
| `plan` | Compare explicitly selected models with an existing schema without applying changes |
| `validate` | Review migration readiness |
| `history`, `status` | Read applied migration history or status |
| `sync` (alias `s`) | Preview changes, or enter the separate approved apply workflow |
| `migration create` | Save an advisory JSON review file; does not execute SQL |
| `migration show` | Verify and display a saved review |
| `migration apply` | **Write to the database** using an approved review and backup |

Read-only inspection commands are not a substitute for a backup before any subsequent write operation.

## Configuration and explicit models

The default configuration path is `aurora_databases.json`. Database commands generally accept `--config` to select a different configuration file and `--database` to select an alias (default: `default`). Configuration and model references must be valid for the project; Aurora does not silently invent missing database files or models.

The model manifest defaults to `aurora_models.json`. Commands that compare or initialize models accept repeatable `--model MODULE:CLASS` references, or can use the project manifest. Model registration does **not** itself create or migrate tables. Generated MVC projects start with an empty manifest (`{"databases": {}}`), so supply `--model` or register a model before running `db init`.

For example, to register an explicit model reference:

```bash
aurora model register myapp.models:Article
aurora model registered
```

Replace the illustrative module and class with a real importable Aurora model in your project.

## Initialize a new SQLite or MySQL database

Run from the **generated project root**, not its `models/` subdirectory. The generated `aurora_databases.json` normally configures the `default` alias with `sqlite:///data.sqlite3`. Create your Aurora model first; for a class `Users` defined in `models/users.py`, run:

```bash
aurora db init --model models.users:Users
```

Alternatively, register it once and then initialize:

```bash
aurora model register models.users:Users
aurora model registered
aurora db init
```

The import reference is case-sensitive. `models.users:Users` means the `Users` class inside the `models.users` module. The CLI resolves project-local model imports from the working directory. A repeated `db init` reports already initialized when all selected tables already exist; it does **not** compare or repair their schema. Use `aurora db plan --model models.users:Users` for supported schema comparison.

**Safety boundary:** `db init` supports SQLite and MySQL with explicitly selected models. It refuses partially initialized databases or unrelated existing tables and never performs migrations. Always verify the configured alias and database path; use a disposable database for first experiments.

### MySQL initialization and inspection (Alpha 2)

MySQL requires the optional `mysql` dependency (PyMySQL) and a configured `mysql+pymysql://` database alias. Use a dedicated account and disposable schema. Store connection credentials outside version control; URL-encode reserved password characters when writing a URL string. Run model-dependent commands from the project root.

```bash
aurora db check --config /path/to/aurora_databases.json --database analytics
aurora db init --config /path/to/aurora_databases.json --database analytics --model myapp.models:Article
aurora db inspect --config /path/to/aurora_databases.json --database analytics
```

The MySQL initializer accepts only an empty schema or a schema whose table *names* match the selected models. Repeating initialization never compares columns, indexes, or constraints and does not prove schema compatibility. MySQL `str` columns require an explicit `length` (for example, `column(str, length=255)`). MySQL DDL generally causes implicit commits: a failure partway through initialization may leave some tables created. Inspect the schema and resolve any partial state manually; do not blindly retry.

`db check` reports table count; `db inspect` reports read-only table metadata (column, index, and foreign-key counts). **Neither verifies schema compatibility.** MySQL `db plan` is a read-only advisory report. MySQL `db status`, migration history, and migration apply are not supported in this alpha; the SQLite migration workflow must not be used against a MySQL alias.

## Safe, read-only workflow

After verifying the database configuration and model references, begin with inspection and planning:

```bash
aurora db list
aurora db show default
aurora db check
aurora db inspect
aurora db plan --model myapp.models:Article --explain
aurora db sync --dry-run --model myapp.models:Article
```

These examples assume a **pre-existing, correctly configured database** and a valid project model. They are not an instruction to create or modify a real database. A missing database, invalid configuration, or incompatible migration history may cause Aurora to refuse the operation.

For CI, `aurora db plan --check` checks for schema drift and may exit with code `2` when drift is detected. `aurora db sync --dry-run --check` provides a dry-run check; `--check` on `sync` requires `--dry-run`.

## Applying migrations requires explicit approval

Aurora's SQLite write workflow is deliberately constrained. It supports reviewed additive changes such as creating tables and adding nullable columns; it does not promise arbitrary destructive schema migrations. Do not assume that dropping tables, renaming columns, or complex data transformations are automatically supported.

Before any write, verify the target alias and file, make an independent backup, review the exact proposed plan, and confirm the plan has not changed. The `sync` apply path requires a reviewed SHA-256 digest, a migration name, a new backup destination, and interactive `APPLY` confirmation. The `migration apply` path requires a saved review file, its exact approval digest, and a new backup path.

**Do not copy an approval digest from this guide or run apply commands against valuable databases as an experiment.** Read the command-specific help and perform initial trials only against disposable test databases. Production-environment checks and incompatible history/schema checks can refuse unsafe operations.

## Further reference

See [CLI reference](cli.md) for the command overview. For precise alpha behavior, inspect `aurora/database/` and the corresponding tests before planning a migration. This page intentionally avoids presenting a universal one-command migration recipe.

### MySQL read-only model compatibility (Alpha 2)

Use `aurora db compat --database analytics --config PATH --model package.models:Model`
from the project root. Repeat `--model` for each selected table, or use
`--models-config` for an explicit per-alias model manifest. This does **not**
create, alter, or migrate tables. It compares selected table/column names,
supported SQL types, nullability, primary keys, indexes, and foreign keys.

The command reports `MATCH` (exit 0), `DRIFT` (exit 2), or `UNVERIFIED`
(exit 3). `MATCH` applies **only to the selected structural checks**, not to
all MySQL schema properties. Server defaults, collations, check constraints,
foreign-key actions, and unselected tables are not fully verified. A difference
takes precedence over an unverified property. MySQL migrations remain
unsupported in this release. Existing `db validate` remains SQLite-oriented. `db plan` supports a separate MySQL read-only advisory mode; neither command enables MySQL migrations.


### MySQL advisory schema planning — Batch 3A

Run from the project root with explicitly selected models:

```bash
aurora db plan --config PATH --database analytics --model package.models:Model
aurora db plan --config PATH --database analytics --model package.models:Model --format json
aurora db plan --config PATH --database analytics --model package.models:Model --check
```

The MySQL branch of `db plan` uses the same conservative comparisons as `db compat`.
It classifies differences as `REVIEW_REQUIRED`, `MANUAL_REVIEW`, or `BLOCKED`;
unsupported comparisons are `UNVERIFIED`. **These labels are advisory, not
permission to execute migrations.** No SQL, review/approval digest, backup,
or migration file is generated. The command does not modify the schema.

`--check` exits 0 for `MATCH`, 2 for `DRIFT`, and 3 for `UNVERIFIED`.
Without `--check`, a successfully generated advisory report exits 0 even
when differences exist. `MATCH` covers selected models and supported checks
only; unselected tables and unsupported MySQL features are outside scope.
SQLite `db plan` behavior is unchanged. MySQL execution and approval design
are deferred to subsequent safety review.

### MySQL migration policy review (Phase 4B, Batch 3B)

`aurora db policy` classifies differences detected by the MySQL advisory planner. It
never produces SQL, accepts an approval, or modifies a schema:

```bash
aurora db policy --config aurora_databases.json --database analytics --model myapp.models:User
aurora db policy --config aurora_databases.json --database analytics --model myapp.models:User --format json
```

Policy decisions are **CANDIDATE** (requires explicit review and a future live
preflight), **MANUAL_REVIEW**, and **BLOCKED**. Missing tables are candidates;
missing columns require manual review because the current planner does not
establish nullability/default preconditions for a safe ADD COLUMN. Unexpected
columns, primary-key changes, unknown and unverified differences are blocked.
A candidate is **not** approval to execute. All policy reports have
`read_only: true`, `executable: false`, `approval_available: false`, and
`execution_available: false`.

Exit status is 0 for MATCH, 2 for DRIFT, and 3 for UNVERIFIED, as with the
read-only MySQL planning check. MySQL DDL may commit implicitly; a later
execution design must address partial failures and concurrent schema changes.
MySQL migrations remain unavailable in this batch.

### MySQL execution readiness (Phase 4B, Batch 3C.1)

`aurora db readiness --database analytics --model myapp.models:MyModel --format json`
performs a read-only assessment of the selected MySQL model/schema policy. The command
accepts the same `--config`, `--database`, `--model`, and `--models-config` options as
`db policy`. It reports `ready_to_execute: false`, `approval_available: false`, and
`execution_available: false` in every outcome. A SHA-256 report fingerprint is
provided for informational comparison **only**: it is not a migration plan identity,
security token, authorization, or approval. A fresh schema check, structured
operation model, and separate approval protocol are still required before any
future MySQL migration executor can be enabled. No SQL is generated or executed.
Exit codes for schema status: MATCH=0, DRIFT=2, UNVERIFIED=3; invalid configuration
is an error. These codes are for CI reporting, **not** permission to execute DDL.
