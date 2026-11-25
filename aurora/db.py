"""Project-scoped AuroraSQL connections and explicit transactions.

A context owns only the connection it opens. It never migrates a database,
creates a missing SQLite file, or changes the model's global legacy binding.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from threading import get_ident
from urllib.parse import quote, unquote, urlsplit

from aurora.database.cli_check import _load_config, resolve_project_database
from aurora.database.connections import connect
from aurora.database.model_discovery import read_model_mapping
from aurora.database.models import Model
from aurora.urls import _current_application

_ACTIVE_SCOPE = ContextVar("aurorasql_project_scope", default=None)


def _project_root():
    app = _current_application.get()
    root = getattr(app, "_project_root", None) if app is not None else None

    if root is None:
        root = Path.cwd()

    root = Path(root).resolve()

    for filename in ("aurora_config.json", "aurora_databases.json"):
        path = root / filename

        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Project database requires a regular {filename} in {root}")

    return root


def _configuration(root, alias):
    config, _ = _load_config(root / "aurora_databases.json")
    selected = resolve_project_database(config, root, alias)
    return config, selected


def _model_alias(root, model):
    manifest = root / "aurora_models.json"

    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError("Automatic model operations require a project aurora_models.json")

    mapping = read_model_mapping(manifest)
    reference = f"{model.__module__}:{model.__name__}"
    matches = [alias for alias, refs in mapping.items() if reference in refs]

    if len(matches) != 1:
        raise ValueError(
            f"Model {reference} must be registered to exactly one database "
            "for automatic AuroraSQL operations"
        )

    return matches[0]


def _alias_for_model(model):
    """Select the model's registered database, without falling back to default."""
    return _model_alias(_project_root(), model)


def _check_scoped_model(model):
    scope = _ACTIVE_SCOPE.get()

    if scope is not None:
        _check_scope_thread(scope)

    if scope is not None and _model_alias(scope[0], model) != scope[1]:
        raise ValueError(
            f"Model {model.__name__} is not registered to scoped database '{scope[1]}'"
        )


def _check_scope_thread(scope):
    if scope[3] != get_ident():
        raise RuntimeError("An AuroraSQL connection scope cannot be used from another thread")


def _project_connection(config, alias, root):
    url = config.resolve(alias)
    parsed = urlsplit(url)

    if parsed.scheme not in ("sqlite", "sqlite+pysqlite"):
        return connect(url)

    if parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("Invalid configured SQLite database URL")

    path = unquote(parsed.path)

    if path == "/:memory:":
        raise ValueError("Project automatic CRUD requires an existing SQLite file")

    if path.startswith("//"):
        location = Path(path[1:])
    elif path.startswith("/"):
        location = root / path[1:]
    else:
        raise ValueError("Invalid SQLite database path")

    if location.is_symlink() or not location.is_file():
        raise ValueError(f"Project database does not exist: {location}")

    return connect("sqlite:///" + quote(location.resolve().as_posix(), safe="/:"))


def _check_sync_context():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return

    raise RuntimeError(
        "Synchronous AuroraSQL CRUD cannot run on the ASGI event loop; "
        "use a synchronous controller or run_database_work()"
    )


@contextmanager
def connection(alias=None):
    """Open, scope and close one project connection, without a transaction."""
    _check_sync_context()
    scope = _ACTIVE_SCOPE.get()
    root = _project_root()

    if scope is not None:
        _check_scope_thread(scope)

    if scope is not None and root != scope[0]:
        raise RuntimeError("Cannot mix different Aurora projects in one connection scope")

    if scope is not None and alias is None:
        alias = scope[1]

    config, selected = _configuration(root, alias)

    if scope is not None and scope[2]._transaction_active:
        if selected != scope[1]:
            raise RuntimeError("Cannot select another database inside a transaction")
        # A nested connection scope borrows the active transaction connection.
        # Only its owner may commit, roll back, or close that connection.
        scope[2]._ensure_open()
        yield scope[2]
        return

    db = _project_connection(config, selected, root)

    with db:
        with Model.connection_scope(db):
            token = _ACTIVE_SCOPE.set((root, selected, db, get_ident()))

            try:
                yield db
            finally:
                _ACTIVE_SCOPE.reset(token)


@contextmanager
def transaction(alias=None):
    """Commit or roll back an explicit transaction on one scoped database."""
    scope = _ACTIVE_SCOPE.get()

    if scope is not None:
        _check_sync_context()
        _check_scope_thread(scope)
        root = _project_root()

        if root != scope[0] or (alias is not None and alias != scope[1]):
            raise ValueError("Transaction must use the currently scoped database")

        with scope[2].transaction():
            yield scope[2]
        return

    with connection(alias) as db:
        with db.transaction():
            yield db


__all__ = ["connection", "transaction"]
