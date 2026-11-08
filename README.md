# Aurora MVC

Aurora MVC is a lightweight, ASGI-first Python web framework for building practical applications with a clear MVC-friendly structure.

**Current source version:** `1.0.0a7` (release candidate; publication requires separate approval). The previous published alpha, [`1.0.0a6`](https://pypi.org/project/aurora-mvc/1.0.0a6/), remains available on PyPI and [GitHub Releases](https://github.com/sigmascript/aurora-mvc/releases/tag/v1.0.0a6). The older `0.9.7` is the last established stable release. This alpha is intended for evaluation, and its APIs and behavior may change.

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

The version-pinned command above works **after** `1.0.0a7` is approved and uploaded to PyPI. Until then, install the source checkout with `python -m pip install -e '.[dev]'`. Optional drivers can be installed with:

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

The six public commands are `aurora db init`, `plan`, `migrate`, `backup`, `restore`, and `history`. **Not every command supports every backend or operation.** `plan` is read-only; schema writes require the supported backend-specific approval gates. **Restore execution is currently disabled** and must not be treated as a recovery solution. Use `aurora db COMMAND --help` and the [database safety guide](docs/database.md) for current constraints.

The migration system supports selected guarded operations, including reviewed repeatable MySQL/PostgreSQL model-driven successors. It is **not a general-purpose schema migration engine**. In particular, non-SQLite explicit JSON schema-operation execution, mixed drift, arbitrary renames, foreign-key changes, primary-key replacement, and destructive transformations are not supported.

## Validation and maturity

The `1.0.0a7` source candidate passed a full Linux Python 3.12 regression of **1,408 passed, 34 skipped, 250 subtests** before release-polish changes; the team also verified **1,405 passed, 37 skipped, 250 subtests** on Windows Python 3.11.9 at the Milestone 4 Batch B acceptance. Disposable MySQL/PostgreSQL Migration3 verification preserved existing rows and confirmed applied history, but did **not** test restoration. Environment-dependent tests are skipped in ordinary runs. These results do not guarantee production suitability; the final release-candidate test results must be recorded before publication.

## Documentation

Start at the [documentation index](docs/index.md), including the [CLI reference](docs/cli.md), [database operations](docs/database.md), and [code style](docs/development/CODE_STYLE.md).

License: MIT. Repository: https://github.com/sigmascript/aurora-mvc
