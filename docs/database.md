# AuroraSQL database operations and safety

This guide describes the integrated **AuroraSQL** backend in the **unreleased Milestone 6 development checkout**, which extends the published `1.0.0a7` alpha. The former SQLAlchemy-backed `aurora.database` implementation is retired. AuroraSQL is alpha software; a command's existence does **not** imply support for every database engine or schema operation. Checkouts and published packages have different capability boundaries.

## Configuration and drivers

Database connections are configured in `aurora_databases.json` using readable `engine` and `database` fields by default; the former `url` and `url_env` settings remain optional. Project models are discovered from shared and application model directories; `aurora_models.json` supports explicit model mappings. Never commit database passwords. SQLite is included; MySQL and PostgreSQL require optional drivers (`PyMySQL` and `psycopg`). This readable format is implemented in the development checkout and still requires Windows acceptance before claiming a released capability.

A normal Aurora project can use just one `default` database. Model directory location is independent of database alias; advanced multi-database configurations remain optional. Every reviewed migration selects exactly one alias.


### Normal SQLite project (generated MVC default)

```json
{
  "databases": {
    "default": {"engine": "sqlite", "database": "data.sqlite3"}
  }
}
```

`default` is a **database alias**, not a backend. The database file is relative to the project folder for CLI database commands. The same alias can select MySQL or PostgreSQL instead.

### MySQL as the default database

```json
{
  "databases": {
    "default": {
      "engine": "mysql",
      "host": "localhost",
      "port": 3306,
      "database": "my_app_dev",
      "username": "app_user",
      "password_env": "APP_DB_PASSWORD"
    }
  }
}
```

### PostgreSQL as the default database

```json
{
  "databases": {
    "default": {
      "engine": "postgres",
      "host": "localhost",
      "port": 5432,
      "database": "my_app_dev",
      "username": "app_user",
      "password_env": "APP_DB_PASSWORD"
    }
  }
}
```

Set `APP_DB_PASSWORD` in your operating system or process environment before using the database commands. For example, in Git Bash, `read -rs APP_DB_PASSWORD; export APP_DB_PASSWORD` reads a password without echoing it or saving it in the shell history. On Windows, you can also configure a process environment variable using your preferred secret tooling. **Do not copy real credentials into JSON, commands, screenshots, or Git.** `host` and `port` here identify the database server, **not Aurora's HTTP/Uvicorn server**. The default database host is `localhost`; MySQL defaults to port 3306 and PostgreSQL to 5432. For simple local-only configuration, use `"password": "your-local-password"` instead of `password_env`; `password_env` means the NAME of an environment variable, **not** the password value. `username` is the only readable credential key; `user` is not accepted. Never commit literal passwords. For MySQL/PostgreSQL, `aurora db init` creates an absent database when the configured user already has the required database-creation privilege; no separate database-creation configuration is required. Aurora never creates server users or grants.

### Copyable PostgreSQL URL (legacy URL-style configuration)

Both readable `"engine": "postgres"` and `"engine": "postgresql"` are accepted. Both `postgresql+psycopg://` (canonical) and `postgres+psycopg://` (short alias) are accepted. They use the same PostgreSQL driver and resolve to the same canonical database identity. The alias names such as `default` or `reports` identify configured databases; they are **not** database engines.

```json
{
  "databases": {
    "default": {
      "url": "postgres+psycopg://postgres:YOUR_LOCAL_PASSWORD@localhost:5432/aurora_mvc_app"
    }
  }
}
```

The example password is a placeholder. Do not commit or share actual passwords. A safer alternative uses the readable PostgreSQL configuration with `password_env` shown above. `postgres://` without `+psycopg` is **not** supported. For a missing target database, `aurora db init` uses the configured account and requires its existing `CREATE DATABASE` privilege; `aurora db check` never creates a database.

### Several distinct databases

```json
{
  "databases": {
    "default": {"engine": "sqlite", "database": "data.sqlite3"},
    "reports": {
      "engine": "postgresql",
      "host": "db.internal",
      "database": "reporting_dev",
      "username": "report_reader",
      "password_env": "REPORTS_DB_PASSWORD"
    }
  }
}
```

Use `aurora db check --database reports` to inspect the selected secondary connection. Configure `aurora_models.json` to associate models with the correct aliases if using explicit registration. Aliases cannot describe the same physical database twice.

### Previous URL style (still optional)

```json
{
  "databases": {
    "default": {"url_env": "APP_DATABASE_URL"}
  }
}
```

A literal `"url"` is also accepted. Illustrative URL shapes are `sqlite:///data.sqlite3`, `mysql+pymysql://user:password@localhost:3306/my_app_dev`, and `postgresql+psycopg://user:password@localhost:5432/my_app_dev` (or `postgres+psycopg://user:password@localhost:5432/my_app_dev`). Prefer `url_env` over embedded credentials. Aurora resolves all forms to its existing internal connection URL representation; **SQLAlchemy is not installed or required for these URLs**. Do not mix a URL field with readable settings within one database entry. No `.env` auto-loading is provided.

### Creating and initializing an absent MySQL or PostgreSQL database

`aurora db init` has the same developer-facing contract across SQLite, MySQL and PostgreSQL: after reviewing and confirming the intended target and model tables, it creates the missing database and initializes its schema, or initializes an already-existing empty database. No extra configuration key or separate maintenance credential is required for the standard path. Existing initialized databases are unchanged. `aurora db check` is always read-only and reports a missing target instead of creating it.

For server databases, Aurora uses the configured `url` / `url_env` or readable database settings. If the target is absent (recognized MySQL `1049` or PostgreSQL `3D000`), `init` reconnects using the **same configured account** to the existing `mysql` (MySQL) or `postgres` (PostgreSQL) bootstrap database on the configured host and port. It verifies the target is absent, displays the exact name and model tables, and requests one confirmation. The configured account must already have the database-creation privilege. Aurora never creates users, changes grants, deletes databases, or silently elevates privileges. If the account lacks the necessary permissions, `init` refuses and the operator must grant only the intended privilege or use an authorized account. This is not a requirement to use an unrestricted administrative account.

```sh
aurora db check           # reports the missing target; no writes
aurora db init            # review and confirm once: create target, initialize tables
aurora db check           # inspect completed state
aurora db history         # inspect forward history
```

The former `provision_url_env` configuration setting has been retired; use the configured account with `aurora db init`. Only simple ASCII target database names (`letters`, `numbers`, `_`) are eligible for automatic server creation. Aurora validates the selected existing bootstrap database identity and checks target absence again immediately before `CREATE DATABASE`. If an attempted creation has an uncertain outcome or schema initialization fails afterward, Aurora retains the database and its evidence; it does not drop or blindly retry. Inspect the resulting state before continuing. The command remains subject to existing production restrictions. **Live MySQL/PostgreSQL acceptance is still pending.**

### Complete MVC developer sequence

From the project directory, after configuring one of the examples and ensuring matching model classes exist:

```sh
aurora db check
# Review the plan and operate only on a database you own.
aurora db init
# Inspect the recorded initial snapshot and physical schema.
aurora db history
aurora db check
aurora db migrate
```

For SQLite, `init` creates an absent database file after confirmation. For MySQL/PostgreSQL, `init` can create the missing database with the already-configured credentials when the account has permission, or initialize an existing empty database; confirm target ownership and authorization first. Live server foreign-key initialization remains **pending authorized practical acceptance**, despite passing offline and Windows contract tests. Existing-table ordinary SQLite index add/drop is supported only through explicit change JSON and verified forward history; FK/UNIQUE/index transformations for other cases remain guarded or refused according to their backend-specific implementation status; never infer universal migration support from the configuration examples.

### Database configuration troubleshooting

`aurora db check` validates the configured database and selected models without creating a schema. Configuration errors should name the setting or correction rather than only an exception class. For example, readable MySQL/PostgreSQL settings require `username` (not `user`); `password` is the literal secret, while `password_env` is the **name** of an environment variable containing it. Likewise, `url` is a complete connection URL and `url_env` is an environment-variable name; use either URL form **or** readable `engine` settings for one database alias. The error message never prints the configured password or full connection URL.

A valid configuration can still fail to connect: missing database, rejected credentials, insufficient privileges and unreachable server are different problems. Aurora reports actionable guidance when the driver supplies a recognized safe error code. For unknown driver/model errors it deliberately withholds the raw exception, which could expose credentials; investigate the selected model, config and server logs securely. A missing server database is never created by `db check`; use `db init` with an account authorized to create the specified target.

## Public CLI

| Command | Purpose | Important limit |
| --- | --- | --- |
| `init` | Initialize discovered model tables | Only supported empty-schema initialization; never use to migrate an existing schema |
| `check` | Inspect database readiness and model differences | Read-only; inspection is not approval to write |
| `migrate` | Preview/apply selected supported schema changes | Backend-specific scope, review, and explicit approval gates; backups optional |
| `backup` | Create a verified backup | SQLite and guarded native MySQL/PostgreSQL paths; backend tools and client prerequisites vary |
| `restore` | Disabled restore command | Execution refused for every backend |
| `history` | Read migration records | Does not itself verify the entire database schema |

There are exactly six public database commands.

## Ordinary model workflow

```sh
aurora db init
aurora db check
aurora db migrate
aurora db history
```

The first `init` command is appropriate only when its target is eligible for initialization. Ordinary interactive migrations review proposed changes and ask for confirmation. Supported non-destructive changes may use `--confirm yes`; populated SQLite table/column deletion additionally requires interactive exact-target confirmation. Add `--database accounting` for a configured alias or `--title "..."` to name a migration where supported. Model discovery does not require a `--model MODULE:CLASS` argument. For an existing MySQL/PostgreSQL database with verified five-column history, the forward migration path supports one independently reviewed server-table transition per execution. It can now combine several ordinary new columns **and eligible existing-column edits** on that table into **one** backend-specific `ALTER TABLE` statement. Eligible existing edits include VARCHAR widening **and data-aware narrowing** (Aurora measures the longest stored text value, accepts narrowing when every value fits, and rechecks under the migration lock); compatible DECIMAL widening and data-aware precision/scale narrowing (only when existing values fit without overflow or rounding); adding, replacing, or removing **verified literal** SQL defaults; and nullable/required transitions (NOT NULL tightening only after an actual-data NULL check). Ordinary additions still require a nullable field or a validated literal default. The reviewer includes observed physical before-state in the exact approval digest and rechecks it under the migration lock. Arbitrary SQL default expressions, ambiguous/unverifiable default metadata, value-converting type changes, indexes/FKs/UNIQUE dependencies, unknown table metadata, and transitions spanning multiple tables are **not** approved by this bounded path. Existing stored values are unchanged by a default replacement or removal; only future inserts use the new declaration. PostgreSQL may report ordinary constants in `pg_get_expr` with known type casts (for example `7::bigint`, `true::boolean`, or `'text'::character varying`); these exact literal forms are recognized for comparison and migration review. SQL functions, arithmetic expressions, nested or unrelated casts, and ambiguous defaults remain refused; MySQL does not accept PostgreSQL cast syntax. On MySQL, eligible edits use one `MODIFY COLUMN` with the complete supported target definition; PostgreSQL uses `ALTER COLUMN` clauses including `DROP DEFAULT` where appropriate. This is distinct from several independent DDL statements: MySQL's nontransactional DDL outcome cannot be assumed atomic across statements. FK/UNIQUE/index-dependent additions, unknown catalog objects and changes on more than one table remain separate review cases. A PostgreSQL failure rolls back its transaction; after MySQL DDL starts, an uncertain result requires physical/history reconciliation rather than an automatic retry. This coherent server-transition extension is offline verified and **not yet accepted against live MySQL/PostgreSQL databases**; history and physical schema must be independently proved before every change. **Migration3 was verified on disposable MySQL and PostgreSQL databases** with existing-row preservation, but no production recovery or universal migration parity is certified. Do **not** invent hashes or bypass history checks.

### Create new SQLite tables after initialization

After `aurora model add Dummy` registers a model, `aurora db check` reports
`create_table` as a pending change (not an unsupported operation). Run
`aurora db migrate --title "Add dummy table"` to review and approve its creation.
An eligible new table is created without resetting the existing database or
history. Multiple new tables with model-declared foreign keys are created in
dependency order, and compatible ordinary column additions to existing tables
may be included in the same approved migration. Existing rows, independently
verified ordinary indexes, foreign-key integrity, and the complete forward
history are preserved. Invalid model dependencies or unsupported changes are
refused before any database writes. `db check` remains read-only.

### Add an ordinary SQLite column to an existing model

Edit the model and use the same model-driven commands, even when the existing table
already has a foreign key:

```python
class Messages(Model):
    id = column(int, primary_key=True)
    user_id = column(int, reference="Users.id")
    visits = column(int, default=0)
```

```sh
aurora db check
aurora db migrate --title "Add message visits"
aurora db check
aurora db history
```

For a populated table, `column(int, default=0)` adds a required column with a
validated database-side default; `column(int, not_null=False)` permits NULL.
A required new column without a default is refused rather than guessing values
for existing rows. This pathway adds **ordinary columns only**; it does not
alter existing foreign keys or introduce a new relationship/UNIQUE constraint.
Aurora verifies physical schema and forward history before and after the edit.

For an existing SQLite column, changing only its database default (for example
`visits = column(int, default=0)` to `visits = column(int, default=10)`)
uses `aurora db check` then `aurora db migrate --title "Change visits default"`.
Existing values remain unchanged; future inserts use the new default.

SQLite model-driven changes are reviewed against the **whole target table**.
For example, these two ordinary declarations can be changed together:

```python
# Existing initialized table:
visits = column(int, default=10)
# Updated model:
visits = column(str, length=10)
```

`aurora db check` reports both the type and default mismatch. A single
`aurora db migrate --title "Convert visits to text"` reviews the stored values,
then changes the type **and removes the old default in one transaction**, with
one resulting `__migrations__` history entry. For an `INTEGER` to `VARCHAR(N)`
transition, non-NULL stored integers must fit in N decimal characters. For a
`VARCHAR` to `INTEGER` transition, stored strings must be canonical decimal
integers in SQLite's 64-bit range; ambiguous strings such as `"007"` are
refused. Existing rows are retained; unverified conversions stop before writes.
Type, default, and nullability changes on **one ordinary application table**
may be reviewed together. `not_null=False` permits NULL; changing it to
`not_null=True` is allowed only after inspecting the current column and proving
that no existing row contains NULL. An empty table is eligible. Moving from
required to nullable preserves existing data. Reviews run again under the
SQLite write lock; an actual NULL blocks the entire transition without a
history record. Unchanged outgoing FKs and verified ordinary indexes survive.
An eligible ordinary **new** column may join these existing-column
changes on the same SQLite table. For example, changing
`visits = column(int, default=10)` to `visits = column(str, length=10)`
while adding `score = column(int, default=7)` is one reviewed table rebuild,
not two migrations. The new column gets its validated SQL default, or NULL
when explicitly nullable; old values are copied and transformed only where
verified safe. A required new column without a default and new PK/UNIQUE/FK
constraints are not eligible for this ordinary addition. Rechecks under the
SQLite write lock, preserved indexes/relationships and one forward snapshot
apply to the full transition; model-only removal is a distinct guarded action,
never inferred as a rename.
Single-column `unique=True` additions/removals are supported in this SQLite
coherent rebuild, including alongside eligible type/default/nullability changes
on the same table. Adding uniqueness first checks real non-NULL values for
duplicates; multiple NULLs remain permitted for nullable UNIQUE columns.
Unchanged outgoing FKs and verified ordinary indexes are preserved, and one
forward snapshot records the entire transition. An inbound foreign key that
could depend on a unique target, self-references, triggers/views, and unverified
or advanced dependencies remain excluded. Composite/conditional UNIQUE,
primary-key changes, renames, removals, and multi-table transitions
are not enabled by this bounded executor.

#### Existing SQLite foreign-key transitions

On a model column that already exists, adding `reference="Users.id"`,
changing its target or `on_update`/`on_delete` action, or removing the
reference can be reviewed through ordinary `aurora db check` and
`aurora db migrate --title "Update message relationship"`. Aurora
validates the resolved target model and key, checks the actual stored
values against a newly selected target (ignoring permitted NULLs), and
requires every non-NULL value to identify an existing referenced row.
An orphan stops the migration before confirmation and leaves the physical
schema and history unchanged. Eligible FK changes can be combined with
supported ordinary type/default/nullability/UNIQUE differences on the
**same table** in one reviewed forward migration. The FK/data review is
repeated under SQLite's write lock; unchanged ordinary indexes and all
application rows are preserved and the new physical FK is verified.

This bounded table rebuild excludes inbound foreign-key dependencies,
self-referencing FKs, triggers/views, unknown indexes and other unverified
objects; it does not silently modify the referenced table. MySQL and
PostgreSQL **existing-table** FK changes are not enabled by this SQLite
implementation. Fresh server FK initialization has a separate pathway
and still requires live backend acceptance.

A `VARCHAR` length reduction (for example `message` from length 500 to 250)
is likewise **data-aware**: an empty column, or one whose longest stored value
fits 250 characters, may be narrowed. Oversized values are reported and the
migration is refused without changing data or history. Compatible widening is
also supported. The executor rechecks the data and physical catalog under a
SQLite write lock, performs a transactional table reconstruction, preserves
unchanged outgoing FKs and verified ordinary indexes, and records one full
forward snapshot. Unknown view/trigger dependencies and inbound or
self-referencing FKs require separate review.

## Explicit schema operations: `aurora db migrate -c`

**Purpose:** Describe an intentional rename or supported ordinary-index change in a small JSON file rather than guessing what a model change meant. The `-c` flag is short for `--change`.

For two column renames on `users`:

```json
{
  "database": "default",
  "rename_columns": {
    "users": {
      "username": "user_name",
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

For an ordinary, nonunique index on an existing `notes` table with `amount` and `title` fields (create one operation file, then apply it):

```json
{
  "database": "default",
  "create_index": {
    "table": "notes",
    "name": "notes_amount_title_idx",
    "columns": ["amount", "title"]
  }
}
```

```sh
aurora db migrate -c create_index.json --title "Index notes amount and title"
# Review and confirm; then verify:
aurora db check
aurora db history
```

For its separately reviewed removal, create another JSON file:

```json
{
  "database": "default",
  "drop_index": {
    "table": "notes",
    "name": "notes_amount_title_idx"
  }
}
```

`aurora db migrate -c drop_index.json --title "Remove notes lookup index"` removes **only** the named ordinary index on the selected application table. Every successful index change records one new `__migrations__` row and a complete physical JSON snapshot. Existing rows are preserved; repeat `check`/`migrate` remains no-op. Indexes must be ordinary, nonunique, model-column based and independently verifiable against existing history. Unique, expression, partial, collated, or otherwise advanced indexes are refused; use the separate approved single-column UNIQUE path where eligible. SQLite and MySQL/PostgreSQL ordinary-index changes use distinct guarded forward-history executors. On the server backends, only an exact existing five-column history and current model snapshots are eligible; PostgreSQL uses transactional DDL, while MySQL reports uncertain outcomes after DDL begins and must never retry automatically. Indexes supporting foreign keys and complex or constraint-backed indexes are not eligible for server removal. **Live server index DDL has not yet been accepted.** SQLite indexes outside verified forward history are not automatically adopted.

One file selects **one operation type and one table**; it can rename several independent columns within that one table. The optional `format` field defaults to `aurorasql.operations.v1` for backward compatibility. On an initialized SQLite database with verified forward history, eligible explicit
column and table renames can also preserve unchanged outgoing or inbound foreign
keys and independently verified ordinary indexes. For verified SQLite forward history, leave the original model declarations
unchanged before the JSON rename: Aurora proposes and validates the corresponding
model-source edits alongside the physical rename (see the source-sync section
below). It checks schema and FK integrity and records one forward-history entry
after the reviewed change. Unsupported dependencies and
inconsistent model/schema correspondence are refused, without guessing renames.

Renames are never inferred automatically. Unknown JSON keys, invalid SQL identifiers, duplicate or chained renames, wrong aliases, and unrelated operations are rejected. The operation file does not contain executable SQL.

### Current public confirmation boundary

`aurora db migrate -c renames.json` selects one explicit JSON operation document. Public migration flags are deliberately minimal; the older `--dry-run`, `--apply`, `--approve`, `--backup` and review-digest arguments are not available. Do not infer broad rename or destructive-operation support from a JSON document. Unsupported operations fail closed; server `create_index` and `drop_index` require verified five-column history and confirmation. Server explicit renames remain refused.


### Guarded model-driven deletion (unreleased checkout only)

For a database with verified **forward-only SQLite history**, removing exactly one existing table or one column from the Python model can be reviewed through ordinary `aurora db check` and `aurora db migrate`. Aurora refuses unsupported dependencies, mixed changes, inconsistent history/snapshots, and changed data after review. Empty tables and columns with no non-NULL values require ordinary migration confirmation; populated tables and columns with existing non-NULL values additionally require typing the **exact table** or **`table.column`** identifier interactively. `--confirm yes` never bypasses that second confirmation. Execution rechecks reviewed data under SQLite's write lock and records approved potential data-loss warnings in `__migrations__.warnings`.

Do **not** interpret this as arbitrary destructive DDL support. Lossy narrowing or constraint-incompatible changes remain refused; MySQL/PostgreSQL table and column deletion is not enabled. Use a disposable database for experimentation, never an important existing database.

## Current limits and recovery

`aurora db restore` always refuses execution, including for an absent SQLite destination. Read-only recovery preflight remains available internally; it does not authorize restoration. The historical MySQL staging-import executor has been retired; read-only recovery preflight does not authorize any restore or staging write. Maintain an independently tested recovery procedure before relying on AuroraSQL for important data.

### Recovery rehearsal design (not an enabled restore feature)

For a future **separately authorized disposable** same-engine recovery rehearsal, the operator must first record the database engine/version, exact source identity and alias, migration timeline state, backup path and integrity/catalog evidence. Select a newly provisioned isolated target with independently verified ownership, privileges and identity; never point recovery at the active application database. Verify the backup was created for the same engine, retain its original bytes, and arrange a separate protected copy for any restore tool. Before permitting any write, require explicit approval naming the disposable target and exact procedure. A read-only preflight is not that approval.

The rehearsal must then compare restored schema, row evidence, migration timeline and operation journal against the recorded source evidence; deliberately exercise a corrupted/incomplete backup refusal, changed-target-identity refusal and uncertain-journal refusal. Any mismatch stops the rehearsal for manual reconciliation without retry or journal repair. Preserve original source and backup evidence, independently confirm that the active database was not modified, and record the observed outcome. **No such live restoration has been authorized or demonstrated.** This design does not enable `aurora db restore`, cross-engine conversion, automated disaster recovery or production certification.

Live backend tests require explicitly disposable databases and specific authorization. Verify connection identity and table ownership before DDL. A successful local regression is not proof of current CI, other database engines, production concurrency or complete recovery readiness. After uncertain MySQL DDL, preserve schema and journal evidence and **never blindly retry**.

### Explicit JSON renames also update Python model source (SQLite forward history)

On a project using the verified SQLite `__migrations__` history, an intentional
column or table rename is declared in a `-c/--change` JSON file. **Leave the
Python model unchanged before running the explicit rename.** Aurora validates
its proposed model-source edit together with a rehearsed physical rename,
requests the normal confirmation, and then updates both the schema and model
source. No rename is inferred from ordinary model-only edits: deleting a field
and introducing a new name are separate removal/addition changes with their
own safety requirements.

Example JSON: `{"database":"default","rename_columns":{"messages":{"user_id":"owner_id"}}}`.
Run `aurora db migrate -c rename_owner.json --title "Rename message owner"`.
Aurora updates the exact `column(...)` assignment in its registered model file,
preserving the remainder of the source. The physical column, values, ordinary
indexes and unchanged foreign keys are checked against the resulting models;
one verified forward-history record is added on success. Literal `reference=` /
`references=` declarations in other registered models are also updated when
they directly identify the renamed FK target, including renamed primary-key
columns and physical table names. Unknown, computed or ambiguous reference
expressions are not rewritten.

For `{"database":"default","rename_table":{"users":"accounts"}}`,
Aurora retains the model's Python class, file and manifest registration, and
sets or updates its `__table__` declaration to the new **physical** name.
References to the stable Python model class continue to resolve through the
validated model registry. It does **not** rename Python classes, files or
unrelated imports automatically. If another source declaration needs an
ambiguous/manual change, the combined review refuses instead of guessing.

Source synchronization supports a uniquely identifiable, project-local,
regular UTF-8 model file and a simple `column(...)` assignment. Existing
source edits are checked again before applying the rename. Known exceptions
roll back the SQLite transaction and restore source edits; files and SQL
transactions are not a single atomic resource across a process crash, so
retain a project backup/source-control recovery path before migration. This
is not a claim of equivalent behavior for MySQL/PostgreSQL or the historical
migration-journal path.

### SQLite forward table removal after removing a model

`aurora model remove Dummy` affects only the registered Python model and its
source; it never deletes the physical `dummy` table or its rows. Afterwards,
`aurora db check` reports the undeclared table and its actual row count or a
specific dependency blocker. `aurora db migrate --title "Remove dummy table"`
may remove exactly one independently verified undeclared table, after approval.
There is **no inferred rename**: ordinary removal remains different from
explicit JSON renaming.

- For an empty table without dependent objects, ordinary migration confirmation
  is sufficient; no automatic backup is required. Its verified ordinary indexes
  are removed along with it. Indexes and FK relationships on unrelated tables
  are retained and verified.
- For a populated table, the migration must first receive normal approval,
  create and verify a SQLite backup under `backups/<database-file-stem>/`, then
  require an **interactive, target-specific** acknowledgement such as
  `DELETE default.dummy`. `--confirm yes` cannot bypass that second gate.
  The backup is retained even after success or a declined acknowledgement.
- Inbound and self-referencing FKs, views/triggers, unverified indexes, changes
  involving multiple removed tables, and mixed destructive/other schema changes
  are refused for further review. A database-history or schema discrepancy
  also prevents the drop. The affected table contents and schema are rechecked
  inside the writer transaction; stale reviews do not execute.
- After a successful drop, Aurora verifies the remaining tables and indexes,
  records one complete forward-history snapshot, and stores the deletion warning
  and backup location when rows were deleted. A repeated migrate is a no-op.

A verified backup is a recovery artifact, **not** automatic restore permission.
Public live restore remains disabled. Always exercise destructive operations on
an explicitly disposable database before considering them in another setting.

### SQLite forward column removal and data protection

A model-only field removal is a request to drop the corresponding physical
column, **never** evidence of a rename. For eligible removed columns whose existing values are all NULL (or their
tables are empty), normal reviewed confirmation suffices. Multiple eligible
removed columns may be approved in one migration with one forward snapshot.

If any non-NULL values would be deleted, Aurora creates **one verified**
pre-migration SQLite backup in `backups/<database-file-stem>/` and requires a
separate interactive acknowledgement for **each populated column**, for example
`DELETE default.messages.comment` and `DELETE default.messages.message`. Generic `--confirm yes` cannot bypass
this risk-specific gate. Failed acknowledgement preserves both the original
database and the verified backup. On success, the history warning identifies
the deleted target and retained backup.

The ordinary `DROP COLUMN` path handles one or more removed columns with no
other schema changes (SQLite 3.35+). **Mixed same-table transitions** may
combine removed columns with eligible type/length, default, nullability, UNIQUE,
FK-action, or ordinary addition changes through the existing transactional
SQLite table rebuild. This is one reviewed migration with one complete forward
snapshot, not an inferred rename. All removed targets are preflighted for actual
data and dependencies, and their review is repeated under the write lock.
A mixed transition uses one verified backup when any removed column contains
non-NULL values, and requires the exact typed acknowledgement for each such
column. A safe mixed transition with no removed values uses ordinary review.
Mixed table removal and changes spread across multiple tables are not supported.
The mixed rebuild does not require SQLite's native `DROP COLUMN` statement. An unsafe target blocks the whole batch. Validated foreign keys and
ordinary indexes **unrelated to the removed column** do not block deletion.
Aurora inspects the actual target: a foreign key using the removed column, an
inbound reference to that column, or an ordinary index using it is a blocker.
Unverified index definitions, constraints enforced by SQLite, views and triggers
still require separate review. After the drop, Aurora checks the preserved
models and ordinary indexes, and verifies its forward-history snapshot.
The writer rechecks data and dependencies under a write lock before execution.
Restore execution remains disabled.

### Server DECIMAL precision/scale reductions (F08)

For an eligible, non-key MySQL/PostgreSQL DECIMAL column, reducing its
precision or scale is reviewed against real stored values. A reduction is
approved only when **no existing value** exceeds the target integral capacity
or requires rounding at the target scale. Both checks run again under the
server migration lock before any DDL. Compatible widening remains supported.
An unsafe or unverifiable change is refused without issuing the ALTER.
This is an offline-verified contract pending authorized live MySQL/PostgreSQL
acceptance; backend-specific DDL/recovery behavior remains unchanged.

### PostgreSQL missing-database connection diagnostics (F08)

`aurora db init` creates an absent configured PostgreSQL database using the
same URL credentials, then initializes its schema after verification and one
reviewed confirmation. Some PostgreSQL startup connection errors lack an
SQLSTATE; Aurora does not assume they mean a missing database. Instead, it
makes an independent read-only connection to the existing `postgres` database,
checks the server catalog for the exact configured target, and permits the
normal reviewed creation path **only when absence is verified**. Authentication
errors, unreachable servers, an existing target, and unverifiable conditions
remain refusals. `aurora db check` and `aurora db history` do not create databases.
