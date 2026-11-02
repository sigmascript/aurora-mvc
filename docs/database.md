# AuroraSQL database operations and safety

This guide describes the integrated **AuroraSQL** backend in the Aurora MVC `1.0.0a6` release candidate. The former SQLAlchemy-backed `aurora.database` implementation is retired. This is alpha software: **the presence of a command does not imply all operations are supported**. Run `aurora db COMMAND --help` for exact options and requirements.

## Configuration and drivers

Database aliases are configured in `aurora_databases.json`. Select models with `--model MODULE:CLASS` or register explicit references in `aurora_models.json`. Where supported, `url_env` reads the connection URL from an environment variable. Never commit database credentials. SQLite is included; MySQL and PostgreSQL require optional drivers (`PyMySQL` and `psycopg`).

## Public CLI

| Command | Purpose | Important limit |
| --- | --- | --- |
| `init` | Initialize explicitly selected model tables | Only supported empty-schema initialization; never use to migrate an existing schema |
| `plan` | Inspect selected schema and report differences | Read-only; inspection is not approval to write |
| `migrate` | Preview/apply selected supported schema changes | Backend-specific scope, review, backup, and explicit approval gates |
| `backup` | Create a verified backup | SQLite and guarded MySQL paths exist; backend support and client prerequisites vary |
| `restore` | Reserved recovery command | **Execution disabled**; refuses unsafe legacy fallback |
| `history` | Read migration records | Does not itself verify the entire database schema |

No additional legacy database command aliases are exposed.

## Safe starting workflow

```sh
aurora db init --model myapp.models:User
aurora db plan --model myapp.models:User
aurora db plan --model myapp.models:User --check
aurora db migrate --model myapp.models:User --dry-run
aurora db history
```

The first `init` command is appropriate only when its target is eligible for initialization. Do not apply it blindly to an existing database. The commands above are illustrative: use `--config`, `--database`, and backend-specific options when required.

For a reviewed migration, follow the exact approval and backup instructions printed by `aurora db migrate --help`. Do **not** invent approval hashes, skip backups, or reuse an approval after schema drift. MySQL DDL may commit independently of migration-history recording; if an outcome is uncertain, inspect the schema and journal before any retry.

## Current limits

The supported migration set is deliberately constrained. Do not assume automatic arbitrary table/column renames, primary-key changes, foreign-key restructuring, destructive transformations, or production-safe recovery. Some schema differences can be detected but not applied. A plan limited to selected models is not proof that unrelated tables or constraints are correct.

`aurora db restore` currently refuses execution. Recovery preflight or staging helper modules do not constitute an enabled, verified restoration workflow. Maintain an independently tested recovery plan before relying on AuroraSQL for important data.

Live backend tests require **disposable databases and explicit authorization**. Never run test DDL against a production or shared application schema.
