# Getting started

Aurora MVC runs on Python 3.10 or newer. This guide follows the current source checkout.

## Install Aurora

From the Aurora repository, install the framework and development tools:

```bash
python -m pip install -e '.[dev]'
```

The development extra includes Uvicorn. Check the installed CLI with `aurora --help`.

## Create your first site

From a directory outside the Aurora source repository:

```bash
aurora new hello_app --mvc
cd hello_app
aurora run
```

Open <http://127.0.0.1:8000/> and stop the server with Ctrl+C. Run `aurora run --no-reload` if you don't want automatic reloads.

You can also create a minimal ASGI project without controllers and templates, or a modular MVC project with separate application components:

```bash
aurora new simple_site
aurora new modular_site --modular
```

## Add a model

MVC and modular projects already include database configuration, but generating a project or model does **not** initialize a database.

```bash
aurora model add User
aurora db check
aurora db init
aurora db history
```

The generated MVC database is named `data` and uses `data.sqlite3`. `init` shows its intended work and asks for approval. Running it again on an initialized database should not create another initial migration.

When your project has more than one configured database, you can select one for a generated model:

```bash
aurora model add Invoice --database accounting
aurora db check --database accounting
```

Add `accounting` to `aurora_databases.json` first. The generator registers the model without connecting to the database. See [AuroraSQL and migrations](database.md) for configuration, safe schema changes, and a [controller-to-model CRUD walkthrough](database.md#use-models-from-application-code). Creating a model registers its class, but you must declare its fields and initialize the schema before storing records.

## Write a route directly

In a minimal project's `app.py`:

```python
from aurora import Aurora

app = Aurora()


def home(request):
    return "Hello from Aurora MVC!"


app.add_route("/", home)
```

Run it from the project directory with `aurora run`, or directly with `python -m uvicorn app:app --reload` if Uvicorn is installed.

## Next steps

Explore [project layouts](project-templates.md), [controllers and routing](routing.md), [application settings](application-configuration.md), and the [CLI reference](cli.md). The full reference lives on [auroramvc.com](https://auroramvc.com).
