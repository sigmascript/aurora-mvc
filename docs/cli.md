# Aurora MVC CLI

Run `aurora --help` to see the available commands. Use `aurora COMMAND --help` for each command’s options.

## Projects and components

```bash
aurora new hello_app                # Minimal ASGI project
aurora new shop --mvc               # MVC layout
aurora new portal --modular         # MVC with reusable apps

aurora controller add Products
aurora controller add Orders --methods "get,post"
aurora view add about
aurora model add Invoice
aurora model add Invoice --database accounting
```

The selected database must exist in the project's configuration. Model generation writes source and model registration only; it does not initialize or migrate databases. For modular components, use `--app APP` where supported. `show` and `list` inspect generated components; removal is guarded by strict-mode policy and additional confirmation.

Run the development server with `aurora run` or `aurora run --no-reload`.

## Database commands

| Command | What it does |
| --- | --- |
| `aurora db check` | Reports readiness and model differences; never writes |
| `aurora db init` | Reviews and initializes an eligible database |
| `aurora db migrate` | Reviews and applies supported forward-only model changes |
| `aurora db backup` | Creates a separate, verified backup when requested |
| `aurora db history` | Lists recorded migrations; `--backups` lists backup records |
| `aurora db restore` | Refuses execution; restore is currently disabled |

Typical workflow:

```bash
aurora db check --database data
aurora db init --database data
aurora db history --database data

# Later, after editing a model:
aurora db check --database data
aurora db migrate --database data --title "Add invoice fields"
aurora db history --database data
```

The `data` name is only an example; use a configured database name. Initialization and migrations ask for approval and refuse unsupported states. Destructive operations may also require a second exact-target acknowledgment. Backups are **optional** and are not created automatically by migrations.

To create a separate backup with a useful label:

```bash
aurora db backup --database data --title "Before invoice update"
aurora db history --database data --backups
```

MySQL backup authentication may need additional local configuration; check `aurora db backup --help`. Do not put passwords in scripts or CLI arguments.

To submit a supported explicit schema operation document:

```bash
aurora db migrate --database data -c changes.json
```

The JSON schema and executable operations are backend-specific; inspect the proposal before confirming. The existence of `-c` does not promise arbitrary schema changes.

### Important limits

- `aurora db plan` and retired migration flags aren't public commands.
- Restore execution is disabled on SQLite, MySQL, and PostgreSQL.
- MySQL DDL may succeed before a subsequent history-recording failure. Aurora must refuse a blind retry and require manual reconciliation when the outcome is uncertain.
- No database command bypasses model, physical-state, or dependency verification merely because an approval flag was supplied.

See [Database operations and safety](database.md) for details.
