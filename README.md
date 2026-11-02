# Aurora MVC

Aurora MVC is a lightweight, ASGI-first Python web framework for building practical applications with a clear MVC-friendly structure.

**Release candidate:** `1.0.0a6` (Alpha 9 architectural cutover). This source snapshot is prepared for release review; it is **not evidence that the version has been published**. Alpha software is subject to API and behavior changes.

## Highlights

- ASGI-first application, routing, requests, responses, and controller-oriented development.
- Project generators for minimal, MVC, and modular layouts.
- Integrated **AuroraSQL** database layer under the public `aurora.database` namespace. The retired SQLAlchemy-backed implementation is no longer the framework's database backend or a required package dependency.
- SQLite support and optional MySQL (`PyMySQL`) and PostgreSQL (`psycopg`) drivers.
- Explicit database configuration, aliases, model selection, and guarded schema operations.

## Installation

Requires Python 3.10 or newer.

```sh
python -m pip install aurora-mvc
```

For a local checkout with development tools:

```sh
python -m pip install -e '.[dev]'
```

Optional database drivers can be installed with `aurora-mvc[mysql]` or `aurora-mvc[postgresql]`.

## Start a project

```sh
aurora new hello_app
cd hello_app
aurora run --no-reload
```

See [Getting started](docs/getting-started.md) for local setup and other layouts.

## Database CLI

The six public commands are `aurora db init`, `plan`, `migrate`, `backup`, `restore`, and `history`. **Not every command supports every backend or operation.** `plan` is read-only; schema writes require the supported backend-specific approval gates. **Restore execution is currently disabled** and must not be treated as a recovery solution. Use `aurora db COMMAND --help` and the [database safety guide](docs/database.md) for current constraints.

The migration system supports selected guarded operations; it is **not yet a complete general-purpose schema migration engine**. In particular, do not assume arbitrary renames, foreign-key changes, primary-key replacement, or destructive transformations are automatically supported.

## Validation and maturity

The Alpha 9 cutover passed the team's recorded full regression: **1,021 passed, 35 intentionally skipped, 247 subtests passed** (2026-11-02). Separate authorized disposable MySQL/PostgreSQL acceptance was reported as **33 passed**. Environment-dependent tests are skipped in ordinary runs. These results do not guarantee suitability for production workloads.

## Documentation

Start at the [documentation index](docs/index.md), including the [CLI reference](docs/cli.md), [database operations](docs/database.md), and [code style](docs/development/CODE_STYLE.md).

License: MIT. Repository: https://github.com/sigmascript/aurora-mvc
