# Database and migration safety

Aurora MVC provides SQLAlchemy-based database interfaces and CLI tools for configuring named databases, registering models, inspecting existing SQLite schemas, and reviewing limited schema migrations. Unreleased Alpha 3 adds separately gated MySQL CREATE TABLE and nullable ADD COLUMN execution. Database operations require more care than the minimal web starter.

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
| `migration apply` | **Write to the SQLite database** using an approved review and backup |
| `mysql-create-table` | Alpha 3: review or explicitly apply one guarded MySQL CREATE TABLE |
| `mysql-add-column` | Alpha 3: review or explicitly apply one guarded MySQL nullable ADD COLUMN |

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

`db check` reports table count; `db inspect` reports read-only table metadata (column, index, and foreign-key counts). **Neither verifies schema compatibility.** MySQL `db plan` is a read-only advisory report. The SQLite `db migration apply` workflow must not be used against a MySQL alias. Alpha 3 has separate MySQL migration-history recording and guarded CLI operations described below; do not infer that SQLite and MySQL share the same migration workflow.

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
takes precedence over an unverified property. The Alpha 2 `compat` command is still read-only; Alpha 3 provides separate, explicitly gated migration commands. Existing `db validate` remains SQLite-oriented. `db plan` supports a separate MySQL read-only advisory mode; neither command enables MySQL migrations.


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
SQLite `db plan` behavior is unchanged. The original advisory planner does not authorize execution; Alpha 3 uses separate guarded commands.

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
This describes the earlier policy-review batch, not the separate Alpha 3 execution commands.

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

## MySQL Alpha 3: guarded CREATE TABLE and ADD COLUMN (development only)

**Availability:** This section describes unreleased `1.0.0a3` development-checkout functionality, not published `1.0.0a2`. The MySQL migration workflow is intentionally narrower than the SQLite workflow. It supports a single new table or a single **nullable** column at a time; it is not a general schema migration engine. No automatic destructive changes, column renames, arbitrary ALTER TABLE operations, or data transformations are authorized.

### Prerequisites and safety boundaries

- Install the optional MySQL dependency (`python -m pip install -e ".[mysql,dev]"` in a development checkout).
- Configure a dedicated MySQL alias in `aurora_databases.json` (or an explicit `--config` file) and identify the **exact schema**. Keep URLs and passwords out of Git, logs, and shared archives.
- Use a disposable database for initial testing. Back up any valuable schema independently and obtain explicit authorization before a write.
- The relevant MySQL migration-history storage must already be initialized through the approved setup procedure. A missing, conflicting, or unverifiable history state must **block** execution; these commands do not silently initialize or repair history.
- The default command mode is review only. A separate execution gate, approval digests, and confirmation are required to attempt DDL.
- MySQL DDL can commit implicitly. DDL and history recording are **not atomic**; a failed history commit or verification can leave an uncertain outcome.

### Phase 1 — Read-only review

From the project root, with an importable model and configured alias, inspect help and run **one** review:

```bash
aurora db mysql-create-table --help
aurora db mysql-add-column --help

aurora db mysql-create-table --config aurora_databases.json --database MY_ALIAS --schema MY_SCHEMA --table MY_TABLE --model package.models:MyModel

aurora db mysql-add-column --config aurora_databases.json --database MY_ALIAS --schema MY_SCHEMA --table MY_TABLE --model package.models:MyModel --column MY_NULLABLE_COLUMN
```

The capitalized names are placeholders. For CREATE TABLE, the target table must be absent and the model/table must match the reviewed operation. For ADD COLUMN, the table must exist, the selected column must be absent, and the model must describe a supported nullable addition. Review the reported schema, exact SQL, SQL SHA-256, operation-specific review SHA-256, and history readiness. A read-only policy or readiness report alone is **not** an approval token.

### Phase 2 — Explicitly approved apply

Only an operator who has independently checked the fresh review, target alias/schema, backup, and migration-history state should authorize an apply. The execution interface requires all of the following **together**:

- Environment gate: `AURORA_MYSQL_MIGRATIONS=1`.
- `--apply` and `--confirm APPLY`.
- `--approve-review` containing the **exact review SHA-256** from the selected operation's review (for ADD COLUMN, the history-review SHA-256).
- `--approve-sql` containing the **exact SQL SHA-256** from that review.

The operator must supply the reviewed values; this guide intentionally does **not** provide a ready-to-run write command or sample approval digests. The executor rechecks the operation, history, and schema while holding a MySQL advisory lock. A stale review, existing/conflicting migration, failed lock acquisition, or unexpected schema state must block execution. Approval is specific to the exact target and proposal; it is not blanket authorization for future migrations.

After CREATE TABLE, the executor verifies the table and persistent migration history. After ADD COLUMN, the hardened executor verifies the resulting column definition (including supported type and nullability) and committed history. The CLI should report success only when the required verification is confirmed.

### Uncertain outcomes and manual reconciliation

**Never automatically retry** an apply after a connection failure, failed commit, failed post-DDL verification, lock-release error, or other uncertain result. MySQL may have committed the DDL even when the CLI reports failure. Do not interpret an exception as proof of rollback, and do not assume that table/column existence alone proves a complete successful migration.

Stop writes to the selected schema. Using a separate read-only connection and an authorized operator, inspect the exact schema/table/column definition and both relevant history records. Compare them with the approved SQL and review digests. If schema and history disagree, treat the outcome as unresolved and escalate for manual reconciliation. Do **not** manually insert history rows or rerun DDL based solely on this guide. Retain the approval/review evidence and a sanitized incident log; never include credentials.

### Existing MySQL read-only tools remain separate

`aurora db compat`, `policy`, `readiness`, and advisory `plan` remain read-only. Their earlier Alpha 2 / pre-execution descriptions above document the history of the design and **do not** override the Alpha 3 execution workflow. SQLite `sync` and `migration apply` remain separate backend-specific workflows.


## Environment-based database URLs (Alpha 4 Batch 3)

Database aliases accept **exactly one** of `url` and `url_env`. Existing `url`
configurations remain compatible. With `url_env`, the environment variable must
exist and contain a valid supported SQLAlchemy URL before configuration loads:

```json
{"databases": {"default": {"url_env": "AURORA_DATABASE_URL"}}}
```

For example, set `AURORA_DATABASE_URL=sqlite:///app.sqlite3` in the process
environment. Environment variables are read when `DatabaseConfiguration` is
constructed, not on every `resolve()` call. Missing or empty variables, invalid
names, unsupported drivers, and ambiguous `url` + `url_env` entries fail closed.
Do not commit credentials, print complete URLs, or put passwords in command history.
This is application configuration, independent of MySQL CLI login-path profiles.
