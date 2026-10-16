# Aurora MVC

Aurora MVC is an ASGI-first Python web framework with an MVC-oriented developer experience.

> **Alpha preparation:** This repository is preparing version `1.0.0a1`. The package has not been published as part of this checkpoint. APIs may change during alpha development.

## Requirements

Python 3.10 or newer.

## Install from a local checkout

```bash
python -m pip install -e '.[dev]'
```

The planned PyPI distribution name is `aurora-mvc`. Python imports use `aurora`, and the CLI command is `aurora`.

## Quick start

Create `app.py`:

```python
from aurora import Aurora

app = Aurora()


def home(request):
    return "Hello from Aurora MVC!"


app.add_route("/", home)
```

Run the development server:

```bash
python -m uvicorn app:app --reload
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
Planned public repository: https://github.com/sigmascript/aurora-mvc
License: MIT (see `LICENSE`).
