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
aurora db init --model myapp.models:User
aurora db plan --model myapp.models:User
aurora db plan --model myapp.models:User --format json --check
aurora db migrate --model myapp.models:User --dry-run
aurora db backup --to backup.sqlite3
aurora db history
```

`--config` selects the database connection configuration (default `aurora_databases.json`); `--database` selects an alias (default `default`). Models may be supplied via repeatable `--model MODULE:CLASS` or an explicit `aurora_models.json` manifest. Missing model selection on SQLite falls back to **inspection only**, not schema verification; `plan --check` returns 3 for this unverified case. An invalid existing manifest is an error.

`plan` never authorizes writes. JSON output includes structured status and safety data. `--check` returns 2 for detected drift and 3 for unverified MySQL or model-free SQLite state.

For SQLite, `migrate --dry-run` previews the existing guarded model migration workflow. Applying an existing reviewed model migration requires the documented digest, review file and explicit approval. MySQL migration is restricted to supported separately guarded operations; uncertain DDL outcomes require manual reconciliation, never blind retry.

### Explicit single-table rename operations (development branch)

`aurora db migrate -c renames.json --dry-run --format json` reads a validated operation document and inspects the target database. The `-c` / `--changes` option is **different** from `--config`, which continues to select database connection settings. Review a digest before considering execution; a preview alone does not authorize DDL. For supported simple SQLite renames, explicit execution requires `--apply --confirm APPLY --approve REVIEW_SHA256 --backup NEW_BACKUP.sqlite3`, with a verified existing intent journal, bounded rehearsal and strict refusal of unsupported dependencies. MySQL/PostgreSQL explicit renames and destructive operations are not implemented.

The JSON format, limitations and examples are documented in [Database and migrations](database.md). This feature is not present in the published `1.0.0a6` package until a subsequent separately approved release.

`restore` is currently disabled by the integrated AuroraSQL CLI. Preflight and staging-related modules are not a supported restore executor.
