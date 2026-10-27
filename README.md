# Aurora MVC

**Aurora MVC** is a lightweight, ASGI-first Python web framework built around a clear MVC structure, simple routing, and practical developer tooling.

> **Release status:** `1.0.0a3` is published on PyPI. `1.0.0a4` is under development in this repository and has not yet been released. Development-checkout features may differ from the published Alpha 3 package.

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

The precise availability and limitations of database operations depend on the backend and release version. **Alpha 3** includes guarded MySQL migration review and explicitly approved execution for one `CREATE TABLE` or one nullable `ADD COLUMN` at a time. This is not a general-purpose MySQL migration engine, and it is subject to the published Alpha 3 safety contract. See the [database safety guide](docs/database.md).

## Alpha 3 MySQL migrations

Install the optional MySQL driver with `python -m pip install -e ".[mysql,dev]"`, configure an explicit MySQL alias and schema, and consult the [MySQL Alpha 3 guide](docs/database.md#mysql-alpha-3-guarded-create-table-and-add-column-development-only) before proceeding. `aurora db mysql-create-table` and `aurora db mysql-add-column` default to review mode; writing requires the explicit migration gate, exact review and SQL digests, `--apply`, and `--confirm APPLY`. MySQL DDL may commit even if subsequent history recording fails. **Never automatically retry an uncertain migration.**

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
python -m unittest discover -s tests
```

Some live database integration tests are opt-in and require explicitly configured disposable databases. Never use a production database for integration tests.

## Project and security notes

- Keep local database URLs, passwords, and environment files outside version control and source archives.
- Review schema-changing operations before execution; MySQL DDL may commit implicitly and cannot be treated as fully rollback-safe.
- Use the current [database safety guide](docs/database.md) before attempting initialization or migration operations.

## License

MIT. See [LICENSE](LICENSE).
