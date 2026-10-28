# Aurora MVC command-line interface

Aurora exposes project scaffolding and six public database commands. For exact flags, run `aurora --help`, `aurora db --help`, or `aurora db COMMAND --help`.

## Database commands

| Command | Purpose | Writes? |
| --- | --- | --- |
| `aurora db init` | Initialize selected model tables in an empty database | Yes, once, with safeguards |
| `aurora db plan` | Inspect and compare selected schema, classify differences, report warnings | No |
| `aurora db migrate` | Preview or apply supported migrations using backend-specific gates | Only after required approvals |
| `aurora db backup` | Create verified database backup | Writes backup file, not schema |
| `aurora db restore` | Guided SQLite recovery or restricted MySQL staging restore | Potentially destructive; explicit safeguards |
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

`--config` selects the database configuration (default `aurora_databases.json`); `--database` selects an alias (default `default`). Models may be supplied via repeatable `--model MODULE:CLASS` or an explicit `aurora_models.json` manifest. Missing model selection on SQLite falls back to **inspection only**, not schema verification; `plan --check` returns 3 for this unverified case. An invalid existing manifest is an error.

`plan` never authorizes writes. Its warnings cover classified differences and limitations of selected-model inspection. For MySQL it includes policy and readiness assessments. JSON output includes structured status and safety data. `--check` returns 2 for detected drift and 3 for unverified MySQL or model-free SQLite state.

For SQLite, `migrate --dry-run` previews the existing guarded migration workflow. Applying a migration requires the documented digest, review file, backup, and explicit approval. MySQL migration is restricted to one supported operation at a time, with schema/table/column targeting, exact review and SQL digests, `--apply`, and `--confirm APPLY`. MySQL DDL may commit before migration-history recording; never blindly retry an uncertain operation. See `aurora db migrate --help`.

`restore` is especially sensitive: SQLite prompts before replacement, while MySQL supports only validated staging workflows, **not production restore**. See `aurora db restore --help` and the database guide.
