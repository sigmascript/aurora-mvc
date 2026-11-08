# Getting started

**Status:** These instructions target the Aurora MVC `1.0.0a7` source release candidate. The previous `1.0.0a6` release is published, but **`1.0.0a7` publication has not been authorized**. The integrated AuroraSQL backend supports bounded reviewed migrations; public restore execution remains disabled.

## Requirements and installation

Use Python 3.10 or newer. Once `1.0.0a7` is published, install the versioned package with `python -m pip install 'aurora-mvc==1.0.0a7'`. Until then, from the root of the Aurora MVC source checkout:

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

## Application settings (optional)

Generated projects include `aurora_config.json`, which defines validated,
nonsecret application defaults such as name and time zone. The generated
`app.py` loads it using `Aurora.from_config(...)`. Existing applications
may continue to construct `Aurora(...)` directly. See
[Application configuration](application-configuration.md).

## Initialize an MVC project database (optional)

The `--mvc` generator includes `aurora_databases.json` (a default SQLite configuration) and an initially empty `aurora_models.json` manifest. Database initialization requires at least one discoverable model. After creating a model class `Users` in `models/users.py`, from the **project root** run:

```bash
aurora db init
aurora db plan
aurora db history --verify-models
```

Models in supported shared/app model directories are discovered automatically across the three generated layouts. If a model lives outside those locations, explicitly register its import reference in the optional manifest:

```bash
aurora model register models.users:Users
aurora model registered
```

`model register` only edits the manifest; it does not create or migrate database tables. Running `db init` again after all selected tables exist reports already initialized without changing the schema. For supported subsequent model changes use `aurora db plan` and interactive reviewed `aurora db migrate`; do not use `init` on partially initialized databases. See the [database guide](database.md).

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
