# MVC Aurora example

Generated with `aurora new --mvc mvc`. Demonstrates a controller and Jinja view.

From this directory run `aurora run --no-reload`, then open `http://127.0.0.1:8000/`.

This is a source example; install Aurora MVC and Uvicorn first.

## Optional SQLite setup

The example includes `aurora_databases.json` with the `default` SQLite alias and an initially empty `aurora_models.json`. It does not initialize tables automatically. After adding a model class such as `Users` in `models/users.py`, run from this directory:

```bash
aurora db init --model models.users:Users
aurora db plan --model models.users:Users
```

To use `aurora db init` without `--model`, first register the reference with `aurora model register models.users:Users`. Initialization is for missing or empty SQLite databases, not schema migrations. See [Database documentation](../../docs/database.md).
