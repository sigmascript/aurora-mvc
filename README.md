# Aurora MVC

**Aurora MVC** is a lightweight, ASGI-first Python web framework built around a clear MVC structure, simple routing, and practical developer tooling.

> **Release status:** `1.0.0a1` is the published alpha. `1.0.0a2` is under development in this repository and is not yet released. Documentation for the development checkout may describe functionality not available in Alpha 1.

## Requirements

- Python 3.10 or newer
- An ASGI server such as Uvicorn to run applications

## Install and get started

Install the published alpha and an ASGI server:

```bash
python -m pip install "aurora-mvc==1.0.0a1" "uvicorn>=0.30"
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

The precise availability and limitations of database operations depend on the backend and release version. **MySQL support is being developed for Alpha 2.** MySQL migration execution is **not** part of the current approved Alpha 2 scope; do not assume SQLite and MySQL have identical migration capabilities.

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
