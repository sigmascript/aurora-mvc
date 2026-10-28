# Command-line interface

The installed console command is `aurora`; the module entry point `python -m aurora` exposes the same top-level command groups. Both were checked on Windows against the current development checkout.

```bash
aurora --help
aurora new --help
aurora run --help
```

## Check the installed version

Both CLI entry points support a version flag:

```bash
aurora --version
aurora -v
python -m aurora --version
```

These commands report the version installed in the active environment. The published Alpha 3 release is `1.0.0a3`; Alpha 4 is under development. The CLI reads installed distribution metadata when available and otherwise falls back to Aurora’s package version.

## Commands at a glance

| Group | Purpose |
| --- | --- |
| `new` | Create a minimal, MVC, or modular project |
| `app` | Add, list, or inspect modular applications |
| `controller` | Add, list, or inspect controllers |
| `view` | Add, list, or inspect views |
| `model` | Add, list, inspect, or register model references |
| `db` | Initialize empty databases, inspect SQLite/MySQL schemas, and manage backend-specific guarded migrations |
| `run` | Start an ASGI application with Uvicorn |

Use `aurora GROUP --help` and, where applicable, `aurora GROUP ACTION --help` for the full argument list.

## Create projects

```bash
aurora new my_site
aurora new my_mvc_site --mvc
aurora new my_modular_site --modular
```

`new` takes the destination project name. It does not overwrite an existing project. The CLI does **not** provide a multilingual project-creation flag; see [Project templates and examples](project-templates.md) for the separate multilingual example.

## Run the development server

From a generated project's root:

```bash
aurora run --no-reload
aurora run --host 127.0.0.1 --port 8080 --no-reload
```

The optional application import target defaults to `app:app`. The default host is `127.0.0.1`, the default port is `8000`, and reload is enabled unless `--no-reload` is supplied. Uvicorn must be installed.

## Work with modular applications and components

```bash
aurora app add news
aurora app list
aurora app show news

aurora controller add Index --app news
aurora controller list --app news
aurora view list --app news
aurora model list --app news
```

Component creation has project-layout requirements. Controllers can be generated in standalone MVC projects or modular apps; shared views and models can also be managed without `--app`. Consult the corresponding subcommand help before modifying a project.

Model registration is separate from creating a model file:

```bash
aurora model register myapp.models:Article
aurora model registered
```

These commands manage explicit model references in the project manifest; they do not automatically apply database migrations.

## Database commands and safety

The `db` group includes `init`, `list`, `show`, `check`, `inspect`, `plan`, `validate`, `history`, `status`, `compat`, `policy`, `readiness`, `mysql-create-table`, `mysql-add-column`, `sync` (alias `s`), and `migration` subcommands. Some are read-only; others can write schema changes when explicitly approved.

For example, with a configured project database:

```bash
aurora db list
aurora db init --model models.article:Article  # new or empty SQLite database only
aurora db plan --model models.article:Article
aurora db sync --dry-run --model myapp.models:Article
```

`db init` is a guarded, one-time operation: it refuses existing partial or unrelated schemas and does not perform migrations. Model references must be importable from the project root.

**Do not use database apply or sync-approval commands against valuable databases without reviewing the generated plan and backup requirements.** See the [database guide](database.md) for the safety model. CLI validation can refuse operations in production environments.

## Alpha 3 guarded MySQL commands

Use `aurora db mysql-create-table --help` and `aurora db mysql-add-column --help` to inspect the exact arguments. Both commands require `--database`, `--schema`, `--table`, and `--model MODULE:CLASS`; `mysql-add-column` also requires `--column`. `--config` defaults to `aurora_databases.json`.

The default invocation is a **read-only review**, for example:

```bash
aurora db mysql-create-table --database MY_ALIAS --schema MY_SCHEMA --table MY_TABLE --model package.models:MyModel
aurora db mysql-add-column --database MY_ALIAS --schema MY_SCHEMA --table MY_TABLE --model package.models:MyModel --column MY_NULLABLE_COLUMN
```

The examples use placeholders and should not be executed unchanged. Only after independent review, an approved disposable target, and explicit operator authorization may a write be attempted. An apply invocation requires `AURORA_MYSQL_MIGRATIONS=1`, `--apply`, `--approve-review` (the exact operation-specific review SHA-256), `--approve-sql` (the exact SQL SHA-256), and `--confirm APPLY`. The `mysql-add-column` review hash is the **history review** digest, not necessarily the CREATE TABLE review digest. The hashes must come from the fresh review of the exact target and must never be guessed or reused.

These commands support **one** guarded CREATE TABLE or **one nullable** ADD COLUMN operation per approval. They do not offer arbitrary schema changes, automatic history repair, or automatic retry. See [database safety and recovery](database.md#mysql-alpha-3-guarded-create-table-and-add-column-development-only).

## Check the actual syntax

The CLI is under active alpha development. For exact arguments, especially for `db migration`, run:

```bash
aurora db --help
aurora db migration --help
aurora db sync --help
aurora db mysql-create-table --help
aurora db mysql-add-column --help
```

## Alpha 4 scaffolding conventions (development checkout)

Generated controller and model filenames preserve CamelCase, for example `controllers/AccountSettings.py` and `models/BlogPost.py`. Inspection accepts the exact generated name and recognizes older lowercase or snake_case filenames without renaming developer files. New page routes use trailing slashes, for example `/account-settings/` and `/blog/news/`. Existing developer-defined routes are not rewritten and remain exact-match. Generated views include a `title` block.


### Alpha 4 database command direction

- `aurora db plan` provides a read-only schema comparison with safety warnings at the end of text output. Existing JSON report shape is preserved.
- `aurora db migrate` is the preferred name for the existing restricted SQLite sync workflow. `aurora db sync` and `aurora db s` remain supported aliases.
- `aurora db migrate --dry-run` previews supported SQLite changes. Applying still requires the exact plan digest, a unique review name, a new backup destination, and interactive `APPLY` confirmation.
- MySQL schema planning is advisory; general MySQL migrations are **not** enabled by the `migrate` alias. Use the existing separately guarded MySQL operations.
- `db plan` now falls back to read-only SQLite schema inspection when neither `--model` nor a model manifest exists. MySQL advisory planning retains its existing behavior. It reports model comparison as UNVERIFIED, and `--check` exits 3 in this case. `db check` remains a compatibility command. An existing invalid manifest is never ignored.
- `aurora db backup` creates a new, verified SQLite snapshot in `backups/` (creating the directory if necessary). Use `--to backups/site.sqlite3` to choose a new filename in an existing directory. Existing backup files are never overwritten.
- `aurora db restore --from backups/site.sqlite3` validates the backup, displays the target and warnings, and requires typing `RESTORE` before replacing the target. The application and all database users **must be stopped for the entire operation**. Restoration refuses WAL mode, journal/WAL sidecars, and observed database locks. A recovery snapshot named `<database>.pre-restore.sqlite3` is created and preserved before replacement; an existing recovery snapshot causes refusal. Non-interactive runs cancel. SQLite locking checks cannot prevent a process starting after validation, so do not restore while the application is running. MySQL production-target recovery is not supported; guarded staging-only MySQL import is available separately. These commands do not roll back migrations.

### MySQL standalone backup (Alpha 4)

`aurora db backup --database default --login-path aurora_recovery` uses the configured
`mysql+pymysql` alias and an existing `mysql_config_editor` login profile.
The profile supplies the password; Aurora passes the configured host, port and user
explicitly to `mysql` and `mysqldump`. Both client tools must be on `PATH`.
A custom destination uses `--to backups/example.sql` and must not exist.
The command refuses non-InnoDB base tables and incomplete dumps, and publishes
completed dumps without overwriting existing files. Backups contain sensitive data.

`--single-transaction` does **not** protect against concurrent DDL. Stop schema
changes while backing up. Consistency is limited to transactional InnoDB data;
server-level objects, users, grants and tablespaces are not included. The dump
requires permissions for routines, triggers and events when present. MySQL production-target restore remains unsupported. A restricted staging-only
import is available for a narrow subset of table/data-only dumps. Dumps containing
routines, triggers, events or views cannot be imported through that mode.

### MySQL restore preflight (Alpha 4; read-only)

`aurora db restore --config aurora_databases.json --database default --from backup.sql --login-path PROFILE --check-only` checks backup file structure, MySQL connection identity, target metadata readability, and staging database name availability. Use `--staging-database NAME` to override the proposed name. This command never executes the backup SQL, creates schemas, or changes records. A staging collision blocks readiness. A footer is **not** proof that SQL is safe or complete. This legacy read-only preflight does not import SQL. A separate guarded staging-only import mode exists; production-target restore and cutover remain unsupported. Never use untrusted dumps. Application downtime, privileges, and non-atomic cutover risks are explicitly unverified.

Future configuration decision (not yet implemented): `url_env` for environment-provided application database URLs, retaining `url` compatibility. Do not commit production passwords. MySQL CLI `--login-path` is separate from PyMySQL application authentication.


### MySQL staging-only restore (Alpha 4 Stage 2)

Use `aurora db restore --config CONFIG --database ALIAS --from BACKUP.sql --login-path SOURCE_PROFILE --staging-database EXISTING_EMPTY_STAGE --staging-login-path RESTRICTED_STAGE_PROFILE --staging-only --check-only` to validate readiness without importing. Omit `--check-only` to request interactive confirmation and staging-only import. Only a conservative subset of Aurora-generated, InnoDB, table/data-only dumps is supported; unfamiliar SQL is refused. No automatic cleanup or cutover occurs. The legacy MySQL `--check-only` preflight without `--staging-only` remains unchanged. Never use a production database as staging.
