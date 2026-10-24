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
| `init` | Initialize a missing or empty SQLite database from explicitly selected models; refuse partial or unrelated schemas |
| `list`, `show` | Inspect configured database aliases and their details |
| `check`, `inspect` | Check or inspect an existing SQLite database |
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

## Initialize a new SQLite database

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

**Safety boundary:** `db init` only supports SQLite and explicitly selected models. It refuses partially initialized databases or unrelated existing tables and never performs migrations. Always verify the configured alias and database path; use a disposable database for first experiments.

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
