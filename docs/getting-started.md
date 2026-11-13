# Getting started

**Status:** These instructions target the **unreleased Milestone 5 development checkout**, not the published `1.0.0a7` package. The public `1.0.0a7` release remains available on PyPI. The newer checkout supports bounded AuroraSQL migrations and an absent-file SQLite restore; MySQL/PostgreSQL restore remains disabled.

## Requirements and installation

Use Python 3.10 or newer. To install the **published** alpha, run `python -m pip install 'aurora-mvc==1.0.0a7'`. To use the newer **unreleased Milestone 5** source, run the following from the repository checkout instead:

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
`app.py` loads the project configuration and conventional resource paths using `Aurora.from_project(__file__)`. Existing applications
may continue to construct `Aurora(...)` directly. See
[Application configuration](application-configuration.md).

## Initialize an MVC project database (optional)

The `--mvc` generator includes `aurora_databases.json` (a default SQLite configuration) and an initially empty `aurora_models.json` manifest. Database initialization requires at least one discoverable model. After creating a model class `Users` in `models/users.py`, from the **project root** run:

```bash
aurora db init
aurora db check
aurora db history
```

Models in supported shared and modular-app model directories are discovered automatically. Optional explicit model selections belong in `aurora_models.json`; the retired `aurora model register` and `registered` commands are not supported.

Re-running `aurora db init` on an initialized database makes no changes. For supported subsequent model changes, use read-only `aurora db check`, then `aurora db migrate` with its confirmation workflow. See the [database guide](database.md).

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
