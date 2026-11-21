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

## Use models from application code

Creating a model registers its Python class with a configured database; it does **not** create its table. After `aurora db init` (or a reviewed migration), import the model class in your controller and call CRUD directly. Aurora opens and closes a short-lived connection for an ordinary registered-model operation.

For example, in an MVC project run `aurora model add Notes`. Edit the generated `models/Notes.py` to declare the columns you need **before** initializing the database. A simple SQLite model could be:

```python
from aurora.database import Model, column


class Notes(Model):
    __table__ = "notes"
    id = column(int, primary_key=True)
    title = column(str)
```

Then use `aurora db check`, review `aurora db init`, and import the class from the package:

```python
from models import Notes

created = Notes.create({"title": "First note"})
note_id = created["id"]
rows = Notes.read({"id": note_id})  # Materialized, list-compatible rows of dictionaries
changed = Notes.update(values={"title": "Edited"}, where={"id": note_id})
deleted = Notes.delete(where={"id": note_id})
```

`create()` takes **one positional mapping** and returns a native dictionary. Keyword-style `create(title="...")` is not supported. `read()` returns a materialized `ModelResult` with `.first()` and `.all()` conveniences; `update()` and `delete()` return affected-row counts. Check those counts when deciding whether to report a missing record. A write with no `where` condition requires the deliberate `confirm=True` safeguard; do not use it for ordinary item updates/deletions.

For operations that must succeed or fail together, use an explicit transaction:

```python
from aurora.db import transaction

with transaction():
    note = Notes.create({"title": "Atomic note"})
    # Other operations for this same configured database can go here.
```

An exception rolls back the transaction. A nested `connection()` scope for the same database borrows the transaction's connection without committing separately. Nested transactions and cross-database borrowing inside a transaction are not supported. Use `with connection():` for an explicitly shared non-transaction scope; caller-owned `connect()` and `.using(db)` remain available for advanced access. Database work is synchronous: in asynchronous handlers, delegate blocking database operations to a supported worker instead of running them directly on the event loop.

### MVC and Modular imports

The managed model exports are package-local:

```python
# MVC, or project-shared models in a Modular project:
from models import Notes

# A model generated for the blog application in a Modular project:
from apps.blog.models import Posts
```

Create an application-local model using `aurora model add Posts --app blog` after creating the `blog` application. Shared models live in the project-level `models/` package; application models live under `apps/blog/models/`. Aurora maintains only the generated package exports on add/remove, preserving unrelated `__init__.py` code. A package import does not choose a database: registration in `aurora_models.json`, and the configured default or named database, determine the target. Use `aurora model add NAME --database ALIAS` where necessary. Identical names across packages may still be refused by existing model/database identity guards; different Python import paths do not override those rules.

### Requests and JSON responses

A synchronous controller can parse form submissions or JSON using `self.request.form()` or `self.request.json()`; asynchronous actions use `await self.request.form()` or `await self.request.json()`. Both are explicit APIs. For example, after creating and initializing `Notes`:

```python
from aurora import Controller
from models import Notes


class NotesApi(Controller):
    def get(self):
        return Notes.read()  # JSON array

    def post(self):
        data = self.request.json()
        title = data.get("title") if isinstance(data, dict) else None
        if not isinstance(title, str) or not title.strip():
            return {"error": "A title is required"}, 400
        return Notes.create({"title": title.strip()}), 201
```

Register it using `app.route("/notes/", NotesApi)` in `app.py`. `get()` and `post()` are discovered automatically. For an HTML form, replace `self.request.json()` with `self.request.form()` and validate the returned fields before writing. In an async controller use `async def post(self)` and `data = await self.request.json()`; follow the worker rule above for synchronous database work.

Returning a dictionary, list, materialized AuroraSQL result or supported `(data, HTTP_status)` tuple produces a JSON response; you can also import `JSONResponse` directly from `aurora` for explicit control. Unsupported values are **not** silently converted to strings. Invalid JSON is rejected with HTTP 400; unsupported form content types with HTTP 415. Request body and upload limits still apply. These snippets show basic inputs, not a complete authentication/CSRF policy: protect state-changing routes, validate ownership and use the application's session/CSRF mechanisms where appropriate.

See [Routing and controllers](routing.md) for HTTP methods and [Project layouts](project-templates.md) for where controllers and models belong.

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
