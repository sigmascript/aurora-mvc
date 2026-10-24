# Command-line interface

The installed console command is `aurora`; the module entry point `python -m aurora` exposes the same top-level command groups. Both were checked on Windows against the current development checkout.

```bash
aurora --help
aurora new --help
aurora run --help
```

## Check the installed version

Both CLI entry points support a version flag:

```bash
aurora --version
aurora -v
python -m aurora --version
```

These commands report the version installed in the active environment. The published Alpha 1 release is `1.0.0a1`; newer source-checkout functionality may not yet be released. The CLI reads installed distribution metadata when available and otherwise falls back to Aurora’s package version.

## Commands at a glance

| Group | Purpose |
| --- | --- |
| `new` | Create a minimal, MVC, or modular project |
| `app` | Add, list, or inspect modular applications |
| `controller` | Add, list, or inspect controllers |
| `view` | Add, list, or inspect views |
| `model` | Add, list, inspect, or register model references |
| `db` | Initialize empty SQLite databases, inspect schemas, preview changes, and manage reviewed migrations |
| `run` | Start an ASGI application with Uvicorn |

Use `aurora GROUP --help` and, where applicable, `aurora GROUP ACTION --help` for the full argument list.

## Create projects

```bash
aurora new my_site
aurora new my_mvc_site --mvc
aurora new my_modular_site --modular
```

`new` takes the destination project name. It does not overwrite an existing project. The CLI does **not** provide a multilingual project-creation flag; see [Project templates and examples](project-templates.md) for the separate multilingual example.

## Run the development server

From a generated project's root:

```bash
aurora run --no-reload
aurora run --host 127.0.0.1 --port 8080 --no-reload
```

The optional application import target defaults to `app:app`. The default host is `127.0.0.1`, the default port is `8000`, and reload is enabled unless `--no-reload` is supplied. Uvicorn must be installed.

## Work with modular applications and components

```bash
aurora app add news
aurora app list
aurora app show news

aurora controller add index --app news
aurora controller list --app news
aurora view list --app news
aurora model list --app news
```

Component creation has project-layout requirements. A controller must target a modular app; shared views and models can also be managed without `--app`. Consult the corresponding subcommand help before modifying a project.

Model registration is separate from creating a model file:

```bash
aurora model register myapp.models:Article
aurora model registered
```

These commands manage explicit model references in the project manifest; they do not automatically apply database migrations.

## Database commands and safety

The `db` group includes `init`, `list`, `show`, `check`, `inspect`, `plan`, `validate`, `history`, `status`, `sync` (alias `s`), and `migration` subcommands. Some are read-only; others can write schema changes when explicitly approved.

For example, with a configured project database:

```bash
aurora db list
aurora db init --model models.article:Article  # new or empty SQLite database only
aurora db plan --model models.article:Article
aurora db sync --dry-run --model myapp.models:Article
```

`db init` is a guarded, one-time operation: it refuses existing partial or unrelated schemas and does not perform migrations. Model references must be importable from the project root.

**Do not use database apply or sync-approval commands against valuable databases without reviewing the generated plan and backup requirements.** See the [database guide](database.md) for the safety model. CLI validation can refuse operations in production environments.

## Check the actual syntax

The CLI is under active alpha development. For exact arguments, especially for `db migration`, run:

```bash
aurora db --help
aurora db migration --help
aurora db sync --help
```
