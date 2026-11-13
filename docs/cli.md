# Aurora MVC command-line interface

Aurora exposes project scaffolding and six public database commands. For exact flags, run `aurora --help`, `aurora db --help`, or `aurora db COMMAND --help`.

## Database commands

| Command | Purpose | Writes? |
| --- | --- | --- |
| `aurora db init` | Initialize selected model tables in an empty database | Yes, once, with safeguards |
| `aurora db check` | Inspect database readiness and model changes without writes | No |
| `aurora db migrate` | Preview or apply supported migrations using backend-specific gates | Only after required approvals |
| `aurora db backup` | Create verified database backup | Writes backup file, not schema |
| `aurora db restore` | Restore eligible SQLite backup into an absent file, with matching snapshots | SQLite only; MySQL/PostgreSQL restore execution refused |
| `aurora db history` | Read applied migration records | No |

There are **no database command aliases**. Legacy commands such as `sync`, `plan`, `inspect`, `status`, `policy`, `readiness`, `compat`, `migration`, `mysql-create-table`, and `mysql-add-column` are removed from the public CLI.

## Examples

```sh
aurora db init
aurora db check
aurora db migrate
aurora db backup --to backup.sqlite3
aurora db history
```

`--database` selects a configured database alias (default `default`). Shared and app models are discovered automatically; `aurora_models.json` optionally maps trusted explicit references. An invalid existing manifest is an error.

`check` is read-only and uses project conventions. It reports readiness, changes, or specific blocked states.

Ordinary interactive `migrate` reviews supported model differences and asks for explicit confirmation after required preflight safeguards; backups are optional. Supported MySQL/PostgreSQL migrations can have repeated reviewed successors; server-side table/column deletion and ambiguous or unsafe changes remain refused. In the unreleased checkout, verified SQLite forward-history databases support narrow single-table or single-column removal with strict interactive second confirmation when data exists. Uncertain DDL outcomes require manual reconciliation, never blind retry. Advanced compatibility flags do not expand the supported operation set.

### Explicit SQLite schema operations

`aurora db migrate -c renames.json` reads a strictly validated operation document for supported SQLite changes and invokes its guarded review/confirmation workflow. The `-c` / `--change` is the explicit JSON operation option. A preview alone never authorizes DDL. MySQL/PostgreSQL explicit JSON schema-operation execution, arbitrary renames and destructive operations are not implemented.

The JSON format, limitations and examples are documented in [Database and migrations](database.md). This guide targets the unreleased Milestone 5 checkout; do not assume its features are present in the published `1.0.0a7` package.

`restore` supports only the bounded absent-file SQLite path with companion snapshot verification. MySQL/PostgreSQL restore execution remains disabled.
