# AuroraSQL: databases and migrations

AuroraSQL is Aurora MVC's built-in database layer. This guide covers the current source checkout.

Aurora supports SQLite, MySQL, and PostgreSQL. Each backend has its own limits. A command being available does not mean every schema change is supported.

## Choose your database

An MVC or modular project has three separate files:

- `aurora_config.json` chooses the project's default database.
- `aurora_databases.json` defines named connections.
- `aurora_models.json` maps registered models to those names.

For a project with three databases, set the default in `aurora_config.json` alongside your other application settings:

```json
{
    "application": {
        "name": "My Shop",
        "timezone": "UTC"
    },
    "default_database": "data",
    "limits": {},
    "constants": {}
}
```

Configure connections in `aurora_databases.json`:

```json
{
    "databases": {
        "data": {
            "engine": "sqlite",
            "extension": "sqlite3"
        },
        "shop": {
            "engine": "mysql",
            "host": "localhost",
            "port": 3306,
            "username": "app_user",
            "password_env": "SHOP_DATABASE_PASSWORD"
        },
        "reports": {
            "engine": "postgresql",
            "host": "localhost",
            "port": 5432,
            "username": "app_user",
            "password_env": "REPORTS_DATABASE_PASSWORD"
        }
    }
}
```

The names identify the databases themselves: `data` is the file `data.sqlite3`, and the MySQL and PostgreSQL database names are `shop` and `reports`. For server databases, Aurora expects an existing account with the required permissions. Set the password environment variables **outside** your committed project files. Optional URL or `url_env` configuration is also supported; don't mix URL and readable forms for the same database.

The generated MVC starter uses `data` as its default. If you configure several connections without `default_database`, supply `--database NAME` explicitly. Database names must be unique within the project, even across engines.

## Register models without touching a database

```bash
aurora model add Customer                  # Project default database
aurora model add Invoice --database shop    # Named MySQL database
aurora model add Report --database reports  # Named PostgreSQL database
```

These commands create Python model files and update `aurora_models.json`. They **do not** connect to a database or create tables. An unknown database or an already-registered model is refused before writing a new model file.

You can inspect a specific database without modifying it:

```bash
aurora db check --database shop
aurora db check --database reports
```

## Initialize and evolve a schema

From a project whose models and database connections are configured:

```bash
aurora db check --database shop
aurora db init --database shop
aurora db history --database shop
```

Initialization reviews the target and intended tables before creating an eligible new schema. It does not silently overwrite an existing one. Subsequent model changes use migrations, not another initialization:

```bash
aurora db check --database shop
aurora db migrate --database shop --title "Add customer preferences"
aurora db history --database shop
```

Migrations are **forward-only**. Successful changes record a complete schema snapshot and a successor history entry. Aurora does not offer a public rollback command.

Changes are reviewed against the selected model declarations, recorded predecessor, actual physical schema, and relevant indexes and constraints. Supported changes include selected additions, field edits, and guarded constraint or removal operations. Eligibility depends on the backend, existing data, and detected dependencies. Aurora refuses combinations or physical states it cannot verify safely.

### Deleting data on purpose

If an operation removes stored values or a populated table, Aurora warns about permanent data loss and requires a specific acknowledgment. It recommends a backup, but **never requires or creates one automatically**. Only approve a proposal that names the exact table or column you intended to remove.

Do not interpret model column removal plus column addition as a rename. Rename requires an explicit, reviewed change document where that operation is supported.

For explicit operations, use:

```bash
aurora db migrate --database data -c changes.json
```

Supported JSON operations and safety checks vary by backend. Review the exact proposal; unsupported operations are refused rather than guessed.

## Create a backup when you choose

```bash
aurora db backup --database shop --title "Before accounting changes"
aurora db history --database shop --backups
```

Aurora assigns a new managed backup identifier and keeps migration backups separate from schema changes. Never put credentials in command lines or committed configuration. Native MySQL/PostgreSQL backup tooling may require additional setup.

`aurora db restore --backup BACKUP_ID` is intentionally **disabled**: the public command refuses execution on every backend. A verified backup artifact is not proof of a successful restore test.

## History, uncertainty, and recovery

- `aurora db check` is read-only; it never approves or executes a migration.
- Migration histories and snapshots belong to their exact configured database. Selecting another default never moves or merges them.
- PostgreSQL supports transactional rollback for eligible reviewed DDL.
- MySQL DDL can commit before a later history step fails. If Aurora reports an uncertain outcome, inspect the physical schema and history. **Do not blindly rerun** the migration.
- SQLite must preserve its physical integrity, foreign keys, and verified predecessor history when changing schema.
- Unknown schema drift, unsupported dependencies, or conflicting evidence are reasons to stop and investigate.

Start with your database's [CLI commands](cli.md) and the current `aurora db COMMAND --help`. For complete model declarations and backend-specific examples, use the reference on [auroramvc.com](https://auroramvc.com).
