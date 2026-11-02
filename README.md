# Aurora MVC

Aurora MVC is a lightweight, ASGI-first Python web framework for building practical applications with a clear MVC-friendly structure.

**Latest published alpha:** `1.0.0a6` (Alpha 9 architectural cutover), available on [PyPI](https://pypi.org/project/aurora-mvc/1.0.0a6/) and [GitHub Releases](https://github.com/sigmascript/aurora-mvc/releases/tag/v1.0.0a6). The older `0.9.7` is the latest stable release; unqualified pip installs normally select stable versions. This alpha is intended for testing and evaluation, and its APIs and behavior may change.

## Highlights

- ASGI-first application, routing, requests, responses, and controller-oriented development.
- Project generators for minimal, MVC, and modular layouts.
- Integrated **AuroraSQL** database layer under the public `aurora.database` namespace. The retired SQLAlchemy-backed implementation is no longer the framework's database backend or a required package dependency.
- SQLite support and optional MySQL (`PyMySQL`) and PostgreSQL (`psycopg`) drivers.
- Explicit database configuration, aliases, model selection, and guarded schema operations.

## Installation

Requires Python 3.10 or newer.

```sh
python -m pip install "aurora-mvc==1.0.0a6"
```

For a local checkout with development tools:

```sh
python -m pip install -e '.[dev]'
```

Optional database drivers for this alpha can be installed with:

```sh
python -m pip install "aurora-mvc[mysql]==1.0.0a6"
python -m pip install "aurora-mvc[postgresql]==1.0.0a6"
```

To allow pip to consider newer prereleases when they become available, use `python -m pip install --pre aurora-mvc`.

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

The Alpha 9 cutover passed the team's recorded full regression: **1,021 passed, 35 intentionally skipped, 247 subtests passed** (2026-11-02). Separate authorized disposable MySQL/PostgreSQL acceptance was reported as **33 passed earlier in Alpha 9, but not rerun during final publication verification**. Environment-dependent tests are skipped in ordinary runs. These results do not guarantee suitability for production workloads.

## Documentation

Start at the [documentation index](docs/index.md), including the [CLI reference](docs/cli.md), [database operations](docs/database.md), and [code style](docs/development/CODE_STYLE.md).

License: MIT. Repository: https://github.com/sigmascript/aurora-mvc
