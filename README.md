# Aurora MVC

Aurora MVC is an ASGI-first Python web framework with an MVC-oriented developer experience.
Simple, Secure, Developer Friendly. 

> **Alpha status:** `aurora-mvc==1.0.0a1` has been published on PyPI. This repository also contains subsequent Alpha 2 development changes that are not necessarily included in the published Alpha 1 package. APIs may change during alpha development.

## Requirements

Python 3.10 or newer.

## Install from a local checkout

```bash
python -m pip install -e '.[dev]'
```

The published PyPI distribution name is `aurora-mvc`. Python imports use `aurora`, and the CLI command is `aurora`. The latest unreleased development changes require an editable source checkout.

## Quick start

For a generated MVC application, use:

```bash
aurora new my_site --mvc
cd my_site
aurora run
```

Database setup is separate: see [Database initialization](docs/database.md#initialize-a-new-sqlite-database).

Alternatively, create `app.py` manually:

```python
from aurora import Aurora

app = Aurora()


def home(request):
    return "Hello from Aurora MVC!"


app.add_route("/", home)
```

Run the development server from the project directory:

```bash
aurora run
```

## CLI

```bash
aurora --help
```

## Examples

The repository includes four framework examples under `examples/`:

- `minimal` — a minimal application
- `mvc` — a controller and Jinja view
- `modular` — separate applications and namespaced views
- `multilingual` — locale-aware routes

Install Aurora MVC and the development dependencies from this checkout first. Then run an example from its own directory:

```bash
cd examples/minimal
aurora run --no-reload
```

See each example's `README.md` for its supported routes and behavior.

## Tests

```bash
python -m unittest discover -s tests -q
```

## Documentation

Start with the [documentation index](docs/index.md) for supported APIs, guides, and current limitations.

## Project

Website: https://auroramvc.com
Repository: https://github.com/sigmascript/aurora-mvc
License: MIT (see `LICENSE`).
