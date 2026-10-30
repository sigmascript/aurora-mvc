# Getting started

**Status:** Aurora MVC `1.0.0a1` is being prepared for alpha release. These instructions use a local source checkout; they do not imply a published PyPI release.

## Requirements and installation

Use Python 3.10 or newer. From the root of the Aurora MVC repository:

```bash
python -m pip install -e '.[dev]'
```

The `dev` extra supplies Uvicorn for the development server. Check the installed CLI with `aurora --help` (or `python -m aurora --help`). Use `aurora --version` to display the installed framework version.

## Create and run your first project

Run the following commands from a directory **outside** the Aurora MVC repository:

```bash
aurora new hello_app
cd hello_app
aurora run --no-reload
```

Open <http://127.0.0.1:8000/>. The default server imports `app:app` from the current project directory. Stop it with Ctrl+C.

For automatic development reloads, use `aurora run` without `--no-reload`.

## Create an application manually

Create an `app.py` file:

```python
from aurora import Aurora

app = Aurora()


def home(request):
    return "Hello from Aurora MVC!"


app.add_route("/", home)
```

From the directory containing that file, run:

```bash
python -m uvicorn app:app --reload
```

## Choose a project structure

The CLI creates three starter layouts:

```bash
aurora new my_site
aurora new my_mvc_site --mvc
aurora new my_modular_site --modular
```

The repository also includes a **multilingual example**, but there is no dedicated `--multilingual` generator flag. See [Project templates and examples](project-templates.md).

## Next steps

Read the [CLI guide](cli.md) for command groups and options, [routing](routing.md) for URL handling, and the [documentation index](index.md) for additional guides. These instructions assume the local checkout is installed; they do not change database schemas or create user accounts.
