# Aurora MVC

Aurora MVC is a lightweight Python web framework built around ASGI, routing, controllers, and straightforward project layouts. Its built-in AuroraSQL layer supports SQLite, MySQL, and PostgreSQL with explicitly reviewed schema changes.

**Latest published alpha:** [`1.0.0a7`](https://pypi.org/project/aurora-mvc/1.0.0a7/) · [GitHub release](https://github.com/sigmascript/aurora-mvc/releases/tag/v1.0.0a7). The current development branch contains newer **unreleased** features. The published package and this checkout may behave differently.

**Release candidate in preparation:** `1.0.0a8` (not yet published).

## Get started

Python 3.10 or newer is required. Install the published alpha:

```bash
python -m pip install "aurora-mvc==1.0.0a7"
```

Or work on the current development checkout:

```bash
python -m pip install -e '.[dev]'
aurora new my_site --mvc
cd my_site
aurora run
```

Visit <http://127.0.0.1:8000/>. For a smaller project without generated controllers or views, use `aurora new my_site` instead. For reusable application components, use `--modular`.

See [Getting started](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/getting-started.md) for the full first-project walkthrough.

## What you can build

- ASGI applications with named routes, HTTP method handling, controllers, Jinja templates, static files, and optional multilingual routing.
- Minimal, MVC, or modular projects with generators for controllers, views, and models.
- Model-driven applications using AuroraSQL, including named SQLite, MySQL, and PostgreSQL connections.
- Reviewed, forward-only schema changes with recorded history. Supported operations depend on the backend and physical schema; unsafe or ambiguous changes are refused.
- Application-controlled sessions, CSRF protection, password helpers, and authentication guards.

The current development checkout lets you register a model with a selected database:

```bash
aurora model add Invoice --database accounting
aurora db check --database accounting
```

That database must already be configured. Generating a model does not create or modify its database.

## Database safety

The public database commands are `check`, `init`, `migrate`, `backup`, `restore`, and `history`. `check` is read-only. Initialization and migrations require the appropriate review and approval; backups are separate and optional, including before destructive changes. Aurora never creates one automatically as part of a migration.

**Restore execution is disabled** on all three backends. MySQL DDL can have an uncertain outcome if a failure occurs after execution begins; inspect the physical schema and history before attempting recovery. See [Database operations](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/database.md) before applying a migration to valued data.

## Documentation

- [Getting started](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/getting-started.md)
- [Documentation index](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/index.md)
- [CLI commands](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/cli.md)
- [Database configuration and migrations](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/database.md)
- [Project layouts](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/project-templates.md)
- [Application settings](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/application-configuration.md)
- [Code style and contributing](https://github.com/sigmascript/aurora-mvc/blob/v1.0.0a8/docs/development/CODE_STYLE.md)

The complete, example-rich reference belongs on [auroramvc.com](https://auroramvc.com); the repository keeps the essential guides close to the code. For work not yet published, see the [Unreleased changelog](https://github.com/sigmascript/aurora-mvc/blob/main/changelog/UNRELEASED.md). Published version notes are available through [GitHub Releases](https://github.com/sigmascript/aurora-mvc/releases).

Aurora MVC is alpha software. Review its supported boundaries before production use.

**License:** MIT · [Repository](https://github.com/sigmascript/aurora-mvc)
