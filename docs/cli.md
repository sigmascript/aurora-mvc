# Aurora MVC command-line interface

Aurora exposes project scaffolding and six public database commands. For exact flags, run `aurora --help`, `aurora db --help`, or `aurora db COMMAND --help`.

## Database commands

| Command | Purpose | Writes? |
| --- | --- | --- |
| `aurora db init` | Initialize selected model tables in an empty database | Yes, once, with safeguards |
| `aurora db plan` | Inspect and compare selected schema, classify differences, report warnings | No |
| `aurora db migrate` | Preview or apply supported migrations using backend-specific gates | Only after required approvals |
| `aurora db backup` | Create verified database backup | Writes backup file, not schema |
| `aurora db restore` | Recovery command reserved for future acceptance | **No: restore execution is disabled** |
| `aurora db history` | Read applied migration records | No |

There are **no database command aliases**. Legacy commands such as `sync`, `check`, `inspect`, `status`, `policy`, `readiness`, `compat`, `migration`, `mysql-create-table`, and `mysql-add-column` are removed from the public CLI.

## Examples

```sh
aurora db init
aurora db plan
aurora db migrate
aurora db backup --to backup.sqlite3
aurora db history
```

`--config` selects the database connection configuration (default `aurora_databases.json`); `--database` selects an alias (default `default`). Shared and app models are discovered automatically; `aurora_models.json` optionally maps trusted explicit references. The older `--model MODULE:CLASS` flag remains available for compatibility, but is not required for ordinary workflow. An invalid existing manifest is an error.

`plan` never authorizes writes. JSON output includes structured status and safety data. `--check` returns 2 for detected drift and 3 for unverified MySQL or model-free SQLite state.

Ordinary interactive `migrate` reviews supported model differences and asks for explicit confirmation after required preflight/backup safeguards. Supported MySQL/PostgreSQL migrations can have repeated reviewed successors, but ambiguous and destructive changes remain refused. Uncertain DDL outcomes require manual reconciliation, never blind retry. Advanced compatibility flags do not expand the supported operation set.

### Explicit SQLite schema operations

`aurora db migrate -c renames.json` reads a strictly validated operation document for supported SQLite changes and invokes its guarded review/confirmation workflow. The `-c` / `--changes` option is **different** from `--config`, which selects database connection settings. A preview alone never authorizes DDL. MySQL/PostgreSQL explicit JSON schema-operation execution, arbitrary renames and destructive operations are not implemented.

The JSON format, limitations and examples are documented in [Database and migrations](database.md). This feature is included in the `1.0.0a7` source candidate, not the older published `1.0.0a6` package.

`restore` is currently disabled by the integrated AuroraSQL CLI. Preflight and staging-related modules are not a supported restore executor.
