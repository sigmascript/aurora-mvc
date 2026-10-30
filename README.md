# Aurora MVC

**Aurora MVC** is a lightweight, ASGI-first Python web framework built around a clear MVC structure, simple routing, and practical developer tooling.

> **Release status:** This checkout is the **unreleased `1.0.0a5` release candidate**. `1.0.0a3` is the latest published version confirmed by this repository documentation; check PyPI for the current published release. Do not assume Alpha 5 is published until the release is approved and uploaded. AuroraSQL is experimental and opt-in; the existing SQLAlchemy-based database layer and six database CLI commands remain the defaults.

## Requirements

- Python 3.10 or newer
- An ASGI server such as Uvicorn to run applications

## Install and get started

Install the published Alpha 3 and an ASGI server:

```bash
python -m pip install "aurora-mvc==1.0.0a3" "uvicorn>=0.30"
```

Inspect the available commands:

```bash
aurora --help
aurora new --help
aurora run --help
```

Create a project using the options shown by `aurora new --help`, change into its directory, and start it using `aurora run`. For the exact supported flags and generated layouts, consult the [getting-started guide](docs/getting-started.md) and [CLI reference](docs/cli.md).

To work with the current development checkout instead:

```bash
python -m pip install -e ".[dev]"
python -m aurora --help
```

Using `python -m aurora` from the checkout helps distinguish local development code from a separately installed global `aurora` command.

## Features

- ASGI-first request handling and routing
- MVC-friendly project structure and controllers
- Templates, static files, and multilingual application support
- Database configuration and model registration using SQLAlchemy
- CLI commands for scaffolding, development, and guarded database workflows

The precise availability and limitations of database operations depend on the backend and release version. The current development checkout exposes six guarded database commands: `init`, `plan`, `migrate`, `backup`, `restore`, and `history`. Backend support and approval requirements vary. See the [database safety guide](docs/database.md).

## Experimental AuroraSQL (Alpha 5)

The `aurora.aurorasql` package is an **experimental, opt-in DB-API implementation** for SQLite, MySQL (PyMySQL), and PostgreSQL (psycopg 3). It is not the default database backend, does not replace `aurora.database`, and is not yet integrated into the existing `aurora db` CLI. Its API and migration behavior may change before a stable release.

The standard installation retains SQLAlchemy for the existing database implementation. Optional database drivers can be installed with `.[mysql]` or `.[postgresql]` when working with AuroraSQL on those backends. Use disposable databases for integration tests; review migration plans and recovery procedures before applying schema changes. Do not assume cross-backend migrations are automatically reversible.

## MySQL migration safety

The published Alpha 3 package provided separate `aurora db mysql-create-table` and `aurora db mysql-add-column` commands. Those are historical Alpha 3 interfaces and are **not available** in the current development CLI.

The current legacy database CLI routes supported guarded operations through `aurora db plan` and `aurora db migrate`. MySQL support remains narrowly scoped to explicitly reviewed operations, including supported `CREATE TABLE` and nullable `ADD COLUMN` cases. Consult the [database safety guide](docs/database.md) for the applicable safety gates and limitations. MySQL DDL can commit even if later verification or history recording fails. **Never automatically retry a migration with an uncertain outcome.**

## Documentation

- [Documentation index](docs/index.md)
- [Getting started](docs/getting-started.md)
- [CLI reference](docs/cli.md)
- [Project templates](docs/project-templates.md)
- [Routing and controllers](docs/routing.md)
- [Database operations and safety](docs/database.md)
- [Multilingual applications](docs/multilingual.md)

Examples are available in the [`examples/`](examples/) directory.

## Development and tests

Install development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run the test suite:

```bash
python -m pytest -q
```

Some live database integration tests are opt-in and require explicitly configured disposable databases. Never use a production database for integration tests.

## Project and security notes

- Keep local database URLs, passwords, and environment files outside version control and source archives.
- Review schema-changing operations before execution; MySQL DDL may commit implicitly and cannot be treated as fully rollback-safe.
- Use the current [database safety guide](docs/database.md) before attempting initialization or migration operations.

## License

MIT. See [LICENSE](LICENSE).
