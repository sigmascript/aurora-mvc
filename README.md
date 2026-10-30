# Aurora MVC

**Aurora MVC** is a lightweight, ASGI-first Python web framework built around a clear MVC structure, simple routing, and practical developer tooling.

> **Alpha preparation:** This repository is preparing version `1.0.0a1`. The package has not been published as part of this checkpoint. APIs may change during alpha development.

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

## Examples

The repository includes four framework examples under `examples/`:

- `minimal` — a minimal application
- `mvc` — a controller and Jinja view
- `modular` — separate applications and namespaced views
- `multilingual` — locale-aware routes

Install Aurora MVC and the development dependencies from this checkout first. Then run an example from its own directory:

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
