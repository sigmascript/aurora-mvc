# Aurora MVC

Aurora MVC is a lightweight, ASGI-first Python web framework for building practical applications with a clear MVC-friendly structure.

**Published alpha:** [`1.0.0a7`](https://pypi.org/project/aurora-mvc/1.0.0a7/) is available on PyPI and [GitHub Releases](https://github.com/sigmascript/aurora-mvc/releases/tag/v1.0.0a7). The current development checkout includes later, **unreleased** Milestone 5 changes; installing `1.0.0a7` does not provide them. The older `0.9.7` is the last established stable release. Alpha APIs and behavior may change.

## Highlights

- ASGI-first application, routing, requests, responses, and controller-oriented development.
- Project generators for minimal, MVC, and modular layouts.
- Integrated **AuroraSQL** database layer under the public `aurora.database` namespace. The retired SQLAlchemy-backed implementation is no longer the framework's database backend or a required package dependency.
- SQLite support and optional MySQL (`PyMySQL`) and PostgreSQL (`psycopg`) drivers.
- Explicit database configuration, aliases, model selection, and guarded schema operations.

## Installation

Requires Python 3.10 or newer.

```sh
python -m pip install "aurora-mvc==1.0.0a7"
```

For a local checkout with development tools:

```sh
python -m pip install -e '.[dev]'
```

The version-pinned command installs the **published** `1.0.0a7` package. To evaluate the newer, unreleased Milestone 5 development source, install its checkout with `python -m pip install -e '.[dev]'`. Optional drivers for the published release can be installed with:

```sh
python -m pip install "aurora-mvc[mysql]==1.0.0a7"
python -m pip install "aurora-mvc[postgresql]==1.0.0a7"
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

The six public commands in the **current development checkout** are `aurora db check`, `init`, `migrate`, `backup`, `restore`, and `history`. The older `db plan` command is retired. **Not every command supports every backend or operation.** `check` is read-only; schema writes require backend-specific safeguards and approval. The newer checkout permits narrowly guarded SQLite restoration into an absent database file with matching snapshots; MySQL/PostgreSQL restore execution is disabled. Use `aurora db COMMAND --help` and the [database safety guide](docs/database.md) for current constraints.

The migration system supports selected guarded operations, including repeatable MySQL/PostgreSQL model-driven successors. The **unreleased development checkout** also supports narrowly guarded single SQLite table/column deletion, with double confirmation for existing data and strict dependency/history verification. It is **not a general-purpose schema migration engine**. In particular, server-side DROP operations, non-SQLite explicit JSON execution, mixed drift, arbitrary renames, foreign-key changes, and primary-key replacement remain unsupported.

## Validation and maturity

For the **unreleased Milestone 5 development checkout**, the team reported **1,593 passed, 37 skipped, 264 subtests passed** on Windows Python 3.11.9 (2026-11-13). Earlier bounded live MySQL/PostgreSQL migration verification preserved existing rows but did **not** demonstrate server restoration. These results are not evidence of production readiness, current CI, or full backend parity. A future release still requires separate acceptance and authorization.

## Documentation

Start at the [documentation index](docs/index.md), including the [CLI reference](docs/cli.md), [database operations](docs/database.md), and [code style](docs/development/CODE_STYLE.md).

License: MIT. Repository: https://github.com/sigmascript/aurora-mvc
