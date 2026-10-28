# Database operations and safety

Aurora MVC supports SQLite and guarded MySQL database workflows. This document describes the Alpha 4 **six-command public CLI**. Use `aurora db COMMAND --help` for the complete supported flags.

## Configuration

Database aliases are configured in `aurora_databases.json`; explicit model references can be registered in `aurora_models.json` or passed with `--model MODULE:CLASS`. `url_env` reads a connection URL from an environment variable at configuration construction time and is mutually exclusive with `url`; do not commit credentials to source control.

## Six commands

1. `aurora db init`: initialize an empty selected schema once; refuse partial or unrelated schemas. Never use it as a migration command.
2. `aurora db plan`: read-only inspection and schema planning with safety warnings. SQLite without models is inspection-only (unverified). MySQL combines schema comparison, policy classification, and readiness assessment; neither mode authorizes execution.
3. `aurora db migrate`: guarded SQLite migration preview/application or one explicitly scoped MySQL operation. All existing review, backup, approval, digest, confirmation, and history protections remain mandatory.
4. `aurora db backup`: verified SQLite backup or MySQL SQL dump using the configured client/login profile.
5. `aurora db restore`: guided SQLite recovery; MySQL restoration is restricted to checked staging targets. Production MySQL restore is not supported.
6. `aurora db history`: inspect applied migrations and optional legacy history file.

All other database CLI commands and aliases are removed, including `sync`, `check`, `inspect`, `status`, `validate`, `compat`, `policy`, `readiness`, `migration`, `mysql-create-table`, and `mysql-add-column`. Their meaningful read-only schema checks now belong to `plan`; guarded execution belongs to `migrate`.

## Safe workflow

```sh
aurora db plan --model myapp.models:User --check
aurora db migrate --model myapp.models:User --dry-run
aurora db backup --to before.sqlite3
aurora db history
```

A plan can be incomplete even when it reports no differences: unselected tables and unsupported database features are not verified. `plan --check` exits 2 on drift and 3 for unverified states where applicable. Never treat a dry run or a policy classification as permission to execute DDL.

SQLite application uses a reviewed digest, existing review directory, explicit backup path, and confirmation gates. MySQL application requires the exact schema and operation, review and SQL hashes, `--apply`, and `--confirm APPLY`; unsupported/destructive operations are not enabled. MySQL DDL may commit before recording history. If execution outcome is uncertain, inspect the live schema and history rather than retrying automatically.

MySQL backups/restores may require local MySQL command-line clients and secure `mysql_config_editor` login paths. Staging restore must use a separate restricted target; never point it at the production schema.
