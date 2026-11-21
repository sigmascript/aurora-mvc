"""Minimal, explicit Aurora developer CLI."""
from __future__ import annotations


import argparse
from importlib.machinery import PathFinder
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import re
import sys

from aurora.core.project_policy import project_is_production

from ._cli_scaffolding import (
    create_project,
    add_app,
    remove_app,
    _modular_apps_directory,
    list_apps,
    show_app,
    _scaffold_app_root,
    _create_scaffold_file,
    _controller_slug,
    _existing_component,
    _reject_component_collision,
    _resolve_component,
    _controller_binding,
    create_controller,
    _standalone_controller_root,
    create_standalone_controller,
    list_standalone_controllers,
    show_standalone_controller,
    create_view,
    create_model,
    list_components,
    show_component,
    _shared_component_root,
    create_shared_component,
    list_shared_components,
    show_shared_component,
    remove_component,
    parse_controller_methods,
)


def _cli_project_is_production(parser, project_root=None):
    """Report missing project policy without exposing configuration internals."""
    try:
        return project_is_production(project_root)
    except PermissionError as error:
        parser.exit(1, f"aurora: {error} No changes were made.\n")


def _database_failure(exc, args, action):
    """Explain project-model failures without leaking database URLs or credentials.

    Only a traceback frame within the configured project is eligible for a
    source location. Database/driver exception text is deliberately hidden.
    """
    # Configuration failures originate in Aurora's own restricted validators.
    # Map only recognized, bounded messages: never echo arbitrary exceptions,
    # driver errors, credentials or connection URLs.
    if type(exc) in (ValueError, TypeError):
        detail = str(exc)
        alias_match = re.fullmatch(r"Database '([a-z][a-z0-9_]*)' (.+)", detail)
        if alias_match:
            alias, reason = alias_match.groups()
            environment = re.fullmatch(
                r"requires (?:a )?nonempty environment variable '([A-Za-z_][A-Za-z0-9_]*)'", reason
            )
            if environment:
                variable = environment.group(1)
                return (f"aurora: Database {action} refused: database '{alias}' needs "
                        f"environment variable '{variable}'. Set it, or use a literal "
                        "password or URL in an untracked local configuration. "
                        "No database changes were made.\n")
            known = {
                "requires engine sqlite, mysql, postgres, or postgresql":
                    "Set 'engine' to sqlite, mysql, or postgresql",
                "must choose password or password_env":
                    "Use either 'password' (literal) or 'password_env' (environment variable name), not both",
                "has an invalid host": "Specify a valid 'host' (default: localhost)",
                "requires a TCP port from 1 to 65535":
                    "Set 'port' to an integer from 1 to 65535",
                "has an invalid database name or username":
                    "Provide a nonempty 'username' without URL delimiters",
                "has an invalid password environment variable name":
                    "Set 'password_env' to a valid environment variable name",
                "has an invalid password": "Set 'password' to a string",
                "must be a mapping": "Define this database using a JSON object",
                "cannot mix URL and readable connection settings":
                    "Use either 'url'/'url_env' or readable 'engine' connection settings, not both",
                "has an invalid environment variable name":
                    "Set 'url_env' to a valid environment variable name",
                "requires a nonempty string URL":
                    "Provide a nonempty 'url' string, or use 'url_env'",
                "has an invalid or unsupported URL":
                    "Use a supported SQLite, MySQL or PostgreSQL connection URL; see docs/database.md",
            }
            if reason == "SQLite accepts only engine and optional extension":
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "SQLite accepts only 'engine' and optional 'extension'. "
                        "No database changes were made.\n")
            if reason == "has unknown connection settings":
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "Remove any unknown fields; server settings require 'username'. "
                        "No database changes were made.\n")
            if reason == "requires username":
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "Set 'username'; remove any unknown fields. "
                        "No database changes were made.\n")
            if reason == "SQLite accepts only engine and optional extension; remove database":
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "Remove the obsolete database field; SQLite uses the configuration name "
                        "and optional extension. No database changes were made.\n")
            if reason == "requires engine and username; remove database":
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "Use the configuration name as the physical database name; "
                        "remove database and provide engine and username. "
                        "No database changes were made.\n")
            if reason in ("URL does not match its configured name", "URL must identify a project-relative SQLite file"):
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "The URL destination must match the configured database name. "
                        "No database changes were made.\n")
            if reason == "has an invalid SQLite extension":
                return (f"aurora: Database {action} refused: database '{alias}': "
                        "SQLite extension must be sqlite3, sqlite, db or empty. "
                        "No database changes were made.\n")
            if reason in known:
                return (f"aurora: Database {action} refused: database '{alias}': "
                        f"{known[reason]}. No database changes were made.\n")
            collision = re.fullmatch(
                r"resolves to the same destination as database '([a-z][a-z0-9_]*)'; use one configured name",
                reason,
            )
            if collision:
                return (f"aurora: Database {action} refused: '{alias}' and "
                        f"'{collision.group(1)}' select the same database. "
                        "Use one configured database name. No database changes were made.\n")
        top_level = {
            "DATABASES must be a mapping": "'databases' must contain a JSON object of named databases",
            "Database names must be lowercase identifiers":
                "Use lowercase database names containing letters, digits or underscores",
            "Cannot read database configuration":
                "Check that aurora_databases.json exists and contains valid JSON",
            "Expected only a databases mapping":
                "The database configuration must contain one top-level 'databases' object",
            "Duplicate model table names":
                "Two selected models use the same table name; rename one or adjust aurora_models.json",
            "Model table name is reserved for migration history":
                "A model uses the reserved history table name; rename its table",
        }
        if detail in top_level:
            return (f"aurora: Database {action} refused: {top_level[detail]}. "
                    "No database changes were made.\n")
    if type(exc) is PermissionError and str(exc).startswith("Project production policy"):
        return f"aurora: Database {action} refused: {exc} No changes were made.\n"

    # Never show raw driver exception text; it may include endpoints or credentials.
    kind = type(exc)
    module = kind.__module__
    mysql_code = (exc.args[0] if exc.args and type(exc.args[0]) is int else None)
    if module.startswith("pymysql"):
        hints = {
            1049: "The selected MySQL database does not exist. Run 'aurora db init' to create and initialize it using the configured account (CREATE DATABASE permission required), then run 'aurora db check'.",
            1045: "MySQL authentication was refused. Check the username, password and database permissions.",
            1044: "The MySQL account lacks access to the selected database. Check its grants.",
            2003: "Cannot connect to MySQL. Check the host, port and whether the server is running.",
            2005: "The MySQL hostname could not be resolved. Check host.",
        }
        if mysql_code in hints:
            return f"aurora: Database {action} failed: {hints[mysql_code]} No database changes were made by this command.\n"
    if module.startswith("psycopg"):
        hints = {
            "3D000": "The selected PostgreSQL database does not exist. Run 'aurora db init' to create and initialize it using the configured account (CREATE DATABASE permission required), then run 'aurora db check'.",
            "28P01": "PostgreSQL authentication was refused. Check the username and password.",
            "42501": "The PostgreSQL account lacks sufficient privileges. Check its grants.",
        }
        code = getattr(exc, "sqlstate", None)
        if code in hints:
            return f"aurora: Database {action} failed: {hints[code]} No database changes were made by this command.\n"

    import traceback

    project = Path(getattr(args, "config", "aurora_databases.json")).resolve().parent
    frames = traceback.extract_tb(exc.__traceback__)
    location = None
    for frame in reversed(frames):
        candidate = Path(frame.filename).resolve()
        if candidate.suffix == ".py" and candidate.is_relative_to(project):
            # Ignore bundled Aurora source when project and repository coincide.
            if "aurora" not in candidate.relative_to(project).parts[:1]:
                location = (candidate.relative_to(project), frame.lineno)
                break

    if isinstance(exc, SyntaxError) and exc.filename:
        candidate = Path(exc.filename).resolve()
        if candidate.is_relative_to(project) and candidate.suffix == ".py":
            location = (candidate.relative_to(project), exc.lineno)

    if location is not None:
        source, line = location
        if isinstance(exc, NameError) and isinstance(getattr(exc, "name", None), str):
            name = exc.name
            if name.isidentifier():
                hint = (" Use unique=True for a unique column." if name == "unique" else
                        " Check that this name is defined or imported.")
                return (f"aurora: Cannot load model in {source}:{line}: "
                        f"name '{name}' is not defined.{hint}\n")
        if isinstance(exc, SyntaxError):
            return f"aurora: Invalid Python syntax in model {source}:{line}; check the declaration.\n"
        return (f"aurora: Cannot load project model in {source}:{line} "
                f"({type(exc).__name__}). Check this model's declarations and imports.\n")

    # These messages are generated by Aurora itself, not by database drivers or
    # user configuration, so actionable hints cannot reveal credentials.
    if type(exc) is ValueError:
        detail = str(exc)
        if detail == "Unknown model-qualified foreign-key target":
            return (f"aurora: Database {action} refused: foreign-key target model is not "
                    "in the selected database model set. Check reference= and aurora_models.json. "
                    "No database changes were made.\n")
        if detail == ("Existing SQLite database is not eligible for initialization; "
                      "review with 'aurora db check' and use 'aurora db migrate' "
                      "for supported model changes"):
            return ("aurora: Database initialization refused: this SQLite database already "
                    "contains schema or history that is not eligible for initialization. "
                    "Run 'aurora db check' to inspect the state; use 'aurora db migrate' "
                    "for supported model changes. No database changes were made.\n")
        if detail == "SQLite database does not exist":
            return (f"aurora: Database {action} refused: SQLite database file does not exist. "
                    "Run 'aurora db check' and initialize it with 'aurora db init' "
                    "before attempting a migration. No database changes were made.\n")
        if detail == ("Extended foreign keys require explicit create-only acceptance; "
                      "migration/init path not supported"):
            return (f"aurora: Database {action} refused: a model declares an extended "
                    "foreign-key relationship, but this initialization/migration route "
                    "does not yet support it. The model was not changed; no DDL was "
                    "executed. Do not remove the relationship to bypass this guard.\n")
    from aurora.database.server_provisioning import ProvisioningUncertain
    if isinstance(exc, ProvisioningUncertain):
        return ("aurora: Database creation outcome is uncertain. Inspect the server "
                "catalog before retrying; Aurora will not delete or retry it automatically.\n")
    if type(exc) is RuntimeError and str(exc) == (
        "Server database was created, but schema initialization did not finish. "
        "Preserve the database and inspect its schema and history; do not delete or blindly retry."
    ):
        return f"aurora: {exc}\n"
    if type(exc) is ValueError and str(exc) in (
        "Database creation requires --confirm yes or an interactive review",
        "Database creation needs an interactive terminal or --confirm yes",
    ):
        return f"aurora: Database {action} refused: {exc}. No database changes were made.\n"
    if isinstance(exc, (ValueError, PermissionError)):
        # Do not echo arbitrary exception text: URLs and credentials may be embedded.
        return (f"aurora: Database {action} refused ({type(exc).__name__}). "
                "Check model declarations, database settings, and supported operations.\n")
    return (f"aurora: Database {action} failed ({type(exc).__name__}). "
            "Check your database connection and model definitions. No automatic retry was attempted.\n")


def _build_parser() -> argparse.ArgumentParser:
    """Declare the public CLI and its unchanged argument contracts."""
    parser = argparse.ArgumentParser(prog="aurora", description="Aurora MVC developer tools")

    try:
        installed_version = version("aurora-mvc")
    except PackageNotFoundError:
        from aurora import __version__

        installed_version = __version__

    parser.add_argument(
        "-v", "--version", action="version", version=f"Aurora MVC {installed_version}"
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="{new,app,controller,view,model,db,run}")
    new = sub.add_parser("new", help="Create a new Aurora project (minimal, --mvc, or --modular)")
    new.add_argument("name", metavar="project", help="New Aurora project directory name")
    new.add_argument("--mvc", action="store_true", help="Create a minimal MVC project")
    new.add_argument("--modular", action="store_true", help="Create a modular MVC project with application components")

    app_group = sub.add_parser("app", help="Add, list, inspect, or safely remove modular applications")
    app_actions = app_group.add_subparsers(dest="action", required=True)
    app_actions.add_parser("add", help="Add a modular application").add_argument("name", metavar="app", help="New modular application name")
    app_actions.add_parser("list", help="List modular applications")
    app_actions.add_parser("show", help="Inspect a modular application").add_argument("name", metavar="app", help="Existing modular application name")
    app_actions.add_parser("remove", help="Remove an untouched generated modular application with confirmation").add_argument("name", metavar="app", help="Existing modular application name")

    for resource in ("controller", "view", "model"):
        group = sub.add_parser(resource, help=f"Manage {resource} components")
        actions = group.add_subparsers(dest="action", required=True)

        for verb in ("add", "list", "show", "remove"):
            action_help = (f"List {resource} components" if verb == "list"
                           else f"{verb.title()} a {resource} component")
            command = actions.add_parser(verb, help=action_help)
            command.add_argument("first", nargs="?" if verb == "list" else None,
                                 metavar="component" if verb != "list" else "app",
                                 help=("Optional modular app to list; omit for shared components" if verb == "list"
                                       else "Component name; in the two-argument form, modular app name"))

            if verb != "list":
                command.add_argument("second", nargs="?", metavar="app_component",
                                     help="Component name when the first argument identifies a modular app")

            command.add_argument("--app", dest="app_option", metavar="APP", help="Target modular application")

            if resource == "controller" and verb == "add":
                command.add_argument("--methods", metavar="VERBS", help="Generate selected HTTP method stubs (comma-separated)")
            if resource == "model" and verb == "add":
                command.add_argument("--database", metavar="NAME", help="Register the model with this configured database")

    # Database commands use explicit safety gates; check is read-only.
    db_group = sub.add_parser("db", help="Check and manage your project databases")
    db_group.description = "Database commands: check, init, migrate, backup, restore, history."
    db_group.epilog = "Normal workflow: aurora db check; aurora db init; aurora db migrate; aurora db history"
    db_actions = db_group.add_subparsers(
        dest="action", required=True,
        metavar="{check,init,migrate,backup,restore,history}",
    )

    # Ordinary checks use project conventions, not caller-supplied manifests.
    check = db_actions.add_parser(
        "check", help="Check database setup and model changes without making changes",
    )
    check.add_argument("--database", default=None, help="Database name from your project settings")
    check.set_defaults(config="aurora_databases.json", models_config="aurora_models.json",
                       model=[], format="text", check=False, explain=False)

    history = db_actions.add_parser("history", help="Show recorded database changes")
    history.add_argument("--database", default=None,
                         help="Database name from your project settings")
    history.add_argument("--backups", action="store_true", help="List verified backup artifacts without modifying storage")
    history.set_defaults(config="aurora_databases.json", models_config="aurora_models.json",
                         history_file=None, intents=False, verify_models=False, format="text")

    backup = db_actions.add_parser(
        "backup", help="Create a verified database backup without overwriting files",
        description=("Back up the selected database to a new file. Existing backups "
                     "are never overwritten. MySQL on Windows uses a configured login-path."),
    )
    backup.add_argument("--database", default=None, help="Database name from project settings")
    backup.add_argument("--title", help="Optional description for the generated backup")
    backup.add_argument("--login-path", help="MySQL authentication profile (Windows)")
    backup.set_defaults(config="aurora_databases.json")

    restore = db_actions.add_parser(
        "restore", help="Restore execution is disabled (always refused)",
        description="Restore execution is disabled for all database backends. No database files are created or modified.",
    )
    restore.add_argument("--database", default=None, help="Database name from project settings")
    restore.add_argument("--backup", required=True, metavar="BACKUP_ID", help="Exact managed backup identifier (restore disabled)")
    restore.set_defaults(config="aurora_databases.json")

    init = db_actions.add_parser(
        "init",
        help="Set up database tables for your models",
        description=(
            "Set up the tables defined by your models. "
            "If the database is already initialized, nothing changes. "
            "If existing tables conflict with your models, Aurora stops without making changes. "
            "Use migrate to update existing tables."
        ),
        epilog=(
            "Examples:\n"
            "  aurora db init\n"
            "  aurora db init --title 'Initial CMS database'\n"
            "  aurora db init --database accounting"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init.add_argument("--approve", help=argparse.SUPPRESS)
    init.add_argument("--confirm", help="Confirm initialization with yes or y")
    init.add_argument("--title", help="Name for this initialization")
    init.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    init.set_defaults(config="aurora_databases.json")
    init.add_argument("--database", default=None, help="Database name from your project settings")
    init.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS", help=argparse.SUPPRESS)
    init.set_defaults(models_config="aurora_models.json")
    sync = db_actions.add_parser('migrate', help='Apply supported model and schema changes')
    sync.add_argument('-c', '--change', dest='changes',
                      help='JSON file describing specific schema changes')
    sync.add_argument('--database', default=None, help='Database name from your project settings')
    sync.set_defaults(
        model=None, config='aurora_databases.json',
        models_config='aurora_models.json', explain=False, format='text',
        dry_run=False, check=False,
    )
    # The public migration interface is intentionally small. Historical
    # execution adapters still inspect these values, but they cannot be
    # supplied from the CLI. Their safety checks remain in the executors.
    sync.set_defaults(
        approve=None, review_file=None, name=None, output_dir="migrations",
        backup=None, schema=None, table=None, column=None, apply=False,
        approve_review=None, approve_sql=None, login_path=None,
    )
    sync.add_argument('--confirm', help='Confirm migration with yes or y')
    sync.add_argument('--title', help='Name for this migration (optional)')
    sync.add_argument('--reconcile-interrupted', metavar='TABLE.COLUMN',
                      help='Explicit history-only recovery of one verified interrupted MySQL column addition')

    # Keep backend-specific limitations visible without implying universal support.
    sync.description = (
        "Update your database to match supported model changes, or use "
        "-c/--change to provide a JSON changes file. "
        "Aurora checks the database before making changes and asks for confirmation. "
        "Unsupported or unsafe changes are refused."
    )

    # Destructive operations remain deliberately unavailable.
    run = sub.add_parser("run", help="Run an ASGI application with Uvicorn")
    run.add_argument("application", nargs="?", default="app:app", help="ASGI import path (default: app:app); run from the project directory")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)
    reload_flags = run.add_mutually_exclusive_group()
    reload_flags.add_argument("--reload", dest="reload", action="store_true", default=None,
                              help="Enable automatic reload")
    reload_flags.add_argument("--no-reload", dest="reload", action="store_false",
                              help="Disable automatic reload")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the canonical Aurora developer commands."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "db" and args.action != "restore" and args.database is None:
        from aurora.database.cli_plan import _load_config, resolve_project_database

        try:
            configured, project_root = _load_config(args.config)
            args.database = resolve_project_database(configured, project_root)
        except (ValueError, TypeError, OSError) as error:
            # Use the established allowlisted, credential-safe diagnostics.
            if str(error) == "Multiple databases configured; set default_database or select --database":
                parser.exit(1, "aurora: Configure default_database or specify --database. No changes made.\n")
            parser.exit(1, _database_failure(error, args, "selection"))

    # AuroraSQL is the only public CLI database engine. Legacy internals remain
    # installed until the retirement acceptance gate, but cannot be selected
    # through a public engine flag or silently receive database operations.
    if args.command == "db" or (args.command == "model" and args.action == "add"):
        args.backend = "aurorasql"

    # Installed console scripts may omit the project directory from sys.path.
    # Explicit database model references must resolve like `python -m aurora`.
    # Restrict this to commands that load project models; help and read-only
    # database listing/inspection do not need to change import resolution.
    if args.command == "db" and args.action in (
        "init", "check", "migrate"
    ):
        project_dir = str(Path.cwd().resolve())

        if project_dir not in sys.path:
            sys.path.insert(0, project_dir)

    if args.command == "db" and args.action in ("backup", "restore") and args.backend == "aurorasql":
        if args.action == "restore":
            parser.exit(1, "aurora: Database restore execution is disabled for all backends. No changes made.\n")

        from aurora.database.cli_plan import _load_config
        from urllib.parse import urlsplit

        try:
            config, _ = _load_config(args.config)
            scheme = urlsplit(config.resolve(args.database)).scheme

            if scheme == "mysql+pymysql":
                if _cli_project_is_production(parser, Path(args.config).expanduser().resolve().parent):
                    raise ValueError("Production backup requires separate approval")

                from aurora.database.cli_mysql_backup import backup_mysql_database
                from aurora.database.backup_artifacts import (prepare_artifact, record_artifact,
                    destination_fingerprint, validate_title)
                validate_title(args.title)
                _, project_root = _load_config(args.config)
                from aurora.database.server_backup_catalog import (inspect_server_catalog,
                                                                     reconcile_server_catalog, _catalog_format)
                from aurora.database.backup_artifacts import list_artifacts
                identity = destination_fingerprint(config.resolve(args.database))
                with config.connect(args.database) as connection:
                    if _catalog_format(connection) == "legacy":
                        raise ValueError("Legacy __backups__ catalog requires an explicitly reviewed transition")
                    reports = inspect_server_catalog(connection, project_root, args.database,
                                                     identity, list_artifacts(project_root, args.database, identity))
                    if any(item["status"] != "ARTIFACT_VERIFIED" for item in reports):
                        raise ValueError("Existing backup evidence requires manual reconciliation")
                backup_id, destination = prepare_artifact(project_root, args.database, "mysql")
                backup_mysql_database(args.config, args.database, str(destination), args.login_path,
                                      expected_url=config.resolve(args.database))
                record_artifact(project_root, args.database, "mysql", backup_id, destination, args.title,
                                destination_fingerprint(config.resolve(args.database)))
                with config.connect(args.database) as connection:
                    reconcile_server_catalog(connection, project_root, args.database, identity)
                print(f"Backup completed: {backup_id}")
                return 0

            if scheme == "postgresql+psycopg":
                if _cli_project_is_production(parser, Path(args.config).expanduser().resolve().parent):
                    raise ValueError("Production backup requires separate approval")

                if args.login_path:
                    raise ValueError("--login-path is specific to MySQL")

                from aurora.database.cli_postgres_backup import backup_postgres_database
                from aurora.database.backup_artifacts import (prepare_artifact, record_artifact,
                    destination_fingerprint, validate_title)
                validate_title(args.title)
                _, project_root = _load_config(args.config)
                from aurora.database.server_backup_catalog import (inspect_server_catalog,
                                                                     reconcile_server_catalog, _catalog_format)
                from aurora.database.backup_artifacts import list_artifacts
                identity = destination_fingerprint(config.resolve(args.database))
                with config.connect(args.database) as connection:
                    if _catalog_format(connection) == "legacy":
                        raise ValueError("Legacy __backups__ catalog requires an explicitly reviewed transition")
                    reports = inspect_server_catalog(connection, project_root, args.database,
                                                     identity, list_artifacts(project_root, args.database, identity))
                    if any(item["status"] != "ARTIFACT_VERIFIED" for item in reports):
                        raise ValueError("Existing backup evidence requires manual reconciliation")
                backup_id, destination = prepare_artifact(project_root, args.database, "postgresql")
                backup_postgres_database(args.config, args.database, str(destination),
                                         expected_url=config.resolve(args.database))
                record_artifact(project_root, args.database, "postgresql", backup_id, destination, args.title,
                                destination_fingerprint(config.resolve(args.database)))
                with config.connect(args.database) as connection:
                    reconcile_server_catalog(connection, project_root, args.database, identity)
                print(f"Backup completed: {backup_id}")
                return 0

            if args.login_path:
                raise ValueError("--login-path is supported only for MySQL")

            from aurora.database.cli_backup import run_backup
            return run_backup(args)
        except FileExistsError:
            parser.exit(1, "aurora: Backup destination already exists. Choose a new filename; no files were overwritten.\n")
        except PermissionError:
            parser.exit(1, "aurora: Backup permission denied. Check source access and destination directory permissions.\n")
        except (ValueError, OSError) as exc:
            # Report known-safe guidance, never raw backend exceptions: connection
            # URLs and client diagnostics can contain credentials.
            detail = str(exc).lower()
            if "login-path" in detail:
                message = "MySQL backup requires a configured --login-path on Windows."
            elif "source file does not exist" in detail:
                message = "SQLite database file does not exist. Check the selected database."
            elif "destination parent directory" in detail:
                message = "Backup destination directory does not exist. Create it first."
            elif "destination must end in .sqlite3" in detail:
                message = "SQLite backup filename must end in .sqlite3."
            elif "new .dump" in detail:
                message = "PostgreSQL backup requires a new .dump file in an existing directory."
            elif "integrity check failed" in detail:
                message = "SQLite integrity check failed; backup was not completed."
            else:
                message = "Backup could not be completed. Check database settings, access, and destination."
            parser.exit(1, f"aurora: {message}\n")
        except RuntimeError as exc:
            detail = str(exc).lower()
            if "migration" in detail or "snapshot" in detail or "database schema differs" in detail:
                parser.exit(1, "aurora: Backup refused: migration history or snapshots are incomplete or inconsistent. Run 'aurora db check' and resolve the problem first.\n")
            parser.exit(1, "aurora: Backup failed unexpectedly; inspect the database and destination.\n")
        except Exception:
            parser.exit(1, "aurora: Backup failed unexpectedly; no legacy fallback was attempted. Inspect the database and destination.\n")

    if args.command == "db" and args.action == "migrate" and args.backend == "aurorasql":
        # Public migration has two reviewed paths: discovered model changes and
        # one explicit JSON specification. Historical digest/review adapters
        # remain internal and cannot be selected using obsolete CLI flags.
        if args.confirm not in (None, "yes", "y"):
            parser.exit(2, "aurora: --confirm accepts only yes or y.\n")

        if getattr(args, 'reconcile_interrupted', None):
            if args.changes or args.title is None or not args.title.strip():
                parser.exit(2, 'aurora: Recovery requires --title and cannot use -c.\n')
            from aurora.database.cli_server_model_migration import run_interrupted_reconciliation
            try:
                return run_interrupted_reconciliation(args)
            except (ValueError, RuntimeError, PermissionError):
                parser.exit(1, 'aurora: History-only reconciliation refused or incomplete. '
                            'Inspect database schema, snapshots and history before retry.\n')

        if args.changes is not None:
            from aurora.database.cli_explicit_operations import run_explicit_operations

            try:
                return run_explicit_operations(args)
            except Exception as exc:
                from aurora.database.explicit_model_rename import ModelSourceRenameError
                if isinstance(exc, ModelSourceRenameError):
                    parser.exit(1, f"aurora: Explicit model-source rename refused: {exc}. No database changes were committed.\n")
                # Only literal, reviewed messages from Aurora's index validators
                # are safe to surface. Never forward raw driver/JSON errors.
                messages = {
                    "Index name is already in use":
                        "An index or schema object already uses this name. Choose another index name or drop the existing ordinary index first.",
                    "Index name already exists":
                        "An index or schema object already uses this name. Choose another index name or drop the existing ordinary index first.",
                    "Only a recorded ordinary index on the selected table may be dropped":
                        "The named ordinary index does not exist on the selected table, or is not eligible for removal. Check the index name and table; do not repeat a completed drop.",
                    "Index target is not a registered application table":
                        "The target table is not registered in aurora_models.json for this database.",
                    "Index column is not declared by the selected model":
                        "The requested index column is not declared by the selected table model.",
                    "Unique indexes require the dedicated UNIQUE migration path":
                        "Ordinary index operations cannot create UNIQUE indexes. Use the supported UNIQUE migration path.",
                }
                guidance = messages.get(str(exc)) if type(exc) is ValueError else None
                if guidance:
                    parser.exit(1, f"aurora: Explicit schema operation refused: {guidance} No database changes were made.\n")
                parser.exit(1, "aurora: Explicit schema operation refused; inspect JSON, alias and schema.\n")

        from aurora.database.cli_model_migration import run_model_migration

        try:
            return run_model_migration(args)
        except Exception as exc:
            from aurora.database.sqlite_forward_history import UnsafeTableTransition
            if isinstance(exc, UnsafeTableTransition):
                parser.exit(1, f"aurora: Model-driven migration refused: {exc}.\n")
            from aurora.database.cli_server_model_migration import UnsupportedServerChange

            if isinstance(exc, UnsupportedServerChange):
                parser.exit(1, f"aurora: Model-driven migration refused. {exc}\n")

            # Only our own validated column-addition refusal is eligible for
            # detailed guidance. Do not expose arbitrary exception/driver text.
            addition = (re.fullmatch(
                r"Cannot add ([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*): "
                r"the column must be nullable or have a database default, "
                r"and cannot introduce a new constraint", str(exc))
                if type(exc) is ValueError else None)
            if addition:
                parser.exit(1, f"aurora: Model-driven migration refused: "
                            f"Cannot add {addition.group(1)}.{addition.group(2)}. "
                            "For existing tables, use column(..., default=VALUE) "
                            "or column(..., not_null=False); adding new constraints "
                            "needs separate migration review. No database changes were made.\n")
            narrowing = (re.fullmatch(
                r"Cannot narrow ([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*) "
                r"to VARCHAR\(([1-9][0-9]*)\): existing value contains "
                r"([1-9][0-9]*) characters; narrowing would truncate data\. "
                r"No database changes were made\.", str(exc))
                if type(exc) is ValueError else None)
            if narrowing:
                table, column, target_length, actual_length = narrowing.groups()
                parser.exit(1, f"aurora: Model-driven migration refused: "
                            f"Cannot narrow {table}.{column} to VARCHAR({target_length}): "
                            f"an existing value has {actual_length} characters. "
                            "Only values fitting the new length are safe; "
                            "the schema and history were not changed.\n")
            diagnostic = _database_failure(exc, args, "migration")
            parser.exit(1, "aurora: Model-driven migration refused. " + diagnostic.removeprefix("aurora: "))

    if args.command == "db" and args.action == "check":
        from aurora.database.cli_plan import run_check
        try:
            return run_check(args)
        except Exception as exc:
            parser.exit(1, _database_failure(exc, args, "check"))

    if args.command == "db" and args.action == "history" and args.backend == "aurorasql":
        if args.history_file:
            parser.exit(1, "aurora: --history-file is for legacy history only.\n")

        if args.intents:
            if args.verify_models:
                parser.exit(1, "aurora: --intents and --verify-models cannot be combined.\n")

            from aurora.database.cli_intent_history import run_intent_history

            try:
                return run_intent_history(args)
            except Exception:
                parser.exit(1, "aurora: SQLite intent history refused; no legacy fallback.\n")

        from aurora.database.cli_history import run_history

        try:
            return run_history(args)
        except Exception as exc:
            # Reuse sanitized Aurora diagnostics; never expose connection URLs.
            parser.exit(1, _database_failure(exc, args, "history"))

    if args.command == "db" and args.action == "init" and args.backend == "aurorasql":
        from aurora.database.cli_init import run_init_check, run_init_apply

        try:
            if args.check:
                if args.approve or args.confirm or args.title is not None:
                    parser.exit(1, "aurora: --check cannot include execution options or a title.\n")

                return run_init_check(args)

            if _cli_project_is_production(parser, Path(args.config).expanduser().resolve().parent):
                parser.exit(1, "aurora: Refusing AuroraSQL initialization in production.\n")

            return run_init_apply(args)
        except ValueError as exc:
            if str(exc) == "At least one model is required":
                parser.exit(1, "aurora: No models found. Add a model with 'aurora model add NAME'.\n")
            parser.exit(1, _database_failure(exc, args, "initialization"))
        except Exception as exc:
            parser.exit(1, _database_failure(exc, args, "initialization"))

    # Route grouped commands to their component handlers.
    if args.command == "app":
        args.command = {"add": "app-add", "list": "app-list", "show": "app-show", "remove": "app-remove"}[args.action]
    elif args.command in ("controller", "view", "model"):
        resource = args.command

        if getattr(args, "second", None) is not None:
            if args.app_option is not None:
                parser.error("Specify the app either positionally or with --app, not both. Example: aurora controller add User --app blog")

            app_name, name = args.first, args.second
        else:
            app_name, name = args.app_option, args.first

            if args.action == "list" and args.first is not None and args.app_option is None:
                app_name, name = args.first, None  # Positional app selector

        if args.action != "list" and not name:
            parser.error(f"{resource} {args.action} requires a component name. Example: aurora {resource} {args.action} "
                         + ("user" if resource == "view" else "User"))

        if args.action in ("add", "remove") and _cli_project_is_production(parser):
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")

        try:
            if args.action == "add":
                methods = parse_controller_methods(args.methods) if resource == "controller" else None

                if resource == "model":
                    from aurora.database.cli_plan import _load_config, resolve_project_database
                    from aurora.database.cli_registration import _load_manifest

                    # Validate identity and manifest before creating a source file.
                    configured, project_root = _load_config(Path.cwd() / "aurora_databases.json")
                    selected = resolve_project_database(configured, project_root, args.database)
                    manifest = _load_manifest(project_root / "aurora_models.json")
                    reference = (f"models.{name}:{name}" if app_name is None else
                                 f"apps.{app_name}.models.{name}:{name}")
                    if any(reference in refs for refs in manifest["databases"].values()):
                        raise ValueError("Model reference already registered; no source file created")

                    from aurora._model_exports import plan_model_export
                    export_folder = (Path.cwd() / "models" if app_name is None else
                                     Path.cwd() / "apps" / app_name / "models")
                    package_export, updated_export, original_export = plan_model_export(export_folder, name)
                    backend = args.backend
                    destination = (create_shared_component("model", name, backend=backend)
                                   if app_name is None else create_model(app_name, name, backend=backend))
                elif app_name is None:
                    destination = (create_standalone_controller(name, methods=methods) if resource == "controller"
                                   else create_shared_component(resource, name))
                else:
                    destination = (create_controller(app_name, name, methods=methods) if resource == "controller"
                               else create_view(app_name, name))

                note = {"controller": " (route registered)", "view": " (routes unchanged)",
                        "model": " (database unchanged; import model explicitly)"}[resource]

                if resource == "model" and backend == "aurorasql":
                    from aurora.database.cli_registration import register_generated_model
                    from aurora._model_exports import write_model_export
                    manifest_file = Path.cwd() / "aurora_models.json"
                    original_manifest = manifest_file.read_bytes()
                    try:
                        reference, _ = register_generated_model(destination, name, alias=selected)
                        write_model_export(package_export, updated_export, expected=original_export)
                    except BaseException:
                        # Undo a partially completed generation, including registration.
                        if manifest_file.read_bytes() != original_manifest:
                            from aurora.database.cli_registration import _save_manifest
                            import json
                            _save_manifest(manifest_file, json.loads(original_manifest))
                        destination.unlink(missing_ok=True)
                        raise
                    note = f" (registered {reference}; database unchanged; exported)"

                print(f"Created {destination}" + note)
            elif args.action == "remove":
                destination = remove_component(resource, name, app_name=app_name)

                if destination is not None:
                    print(f"Removed {destination} (database unchanged)")
            elif args.action == "list":
                entries = (list_components(resource, app_name) if app_name is not None
                           else list_standalone_controllers() if resource == "controller"
                           else list_shared_components(resource))
                print("\n".join(entries) if entries else f"No {resource}s found.")
            else:
                info = (show_component(resource, app_name, name) if app_name is not None
                        else show_standalone_controller(name) if resource == "controller"
                        else show_shared_component(resource, name))
                print(f"{resource.title()}: {info['name']}\nApp: {info['app']}\nPath: {info['path']}")
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        return 0

    if args.command == "new":
        try:
            root = create_project(args.name, mvc=args.mvc, modular=args.modular)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print(f"Created {root}\nRun: cd {args.name} && aurora run")
        return 0

    if args.command == "app-add":
        if _cli_project_is_production(parser):
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")

        try:
            destination = add_app(args.name)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print(f"Created {destination}\nRoute: /{args.name}")
        return 0

    if args.command == "app-remove":
        if _cli_project_is_production(parser):
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")
        try:
            removed = remove_app(args.name)
        except (OSError, ValueError, SyntaxError) as exc:
            parser.exit(1, f"aurora: {exc}\n")
        if removed is not None:
            print(f"Removed {removed} (database unchanged)")
        return 0

    if args.command == "app-list":
        try:
            names = list_apps()
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print("\n".join(names) if names else "No apps found.")
        return 0

    if args.command == "app-show":
        try:
            info = show_app(args.name)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print(f"App: {info['name']}\nPath: {info['path']}")
        print("Controllers: " + (", ".join(info["controllers"]) or "(none)"))
        print("Views: " + (", ".join(info["views"]) or "(none)"))
        return 0

    from aurora.core.app_settings import load_application_settings

    # A dotted local project entry (e.g. news.app:app) uses that project's
    # settings, not unrelated settings from the invoking parent directory.
    # No Python imports or user code execution are needed for this resolution.
    module_name = args.application.partition(":")[0]
    segments = module_name.split(".")
    local_project = (len(segments) >= 2 and all(part.isidentifier() for part in segments)
                     and (Path.cwd() / segments[0]).is_dir()
                     and (Path.cwd() / segments[0] / "__init__.py").is_file())
    project = Path.cwd() / segments[0] if local_project else Path.cwd()
    config = project / "aurora_config.json"
    try:
        if config.exists() or config.is_symlink():
            settings = load_application_settings(config)
            defaults = settings.server
        else:
            defaults = {"host": "127.0.0.1", "port": 8000, "reload": True}
    except (OSError, ValueError) as exc:
        # The settings validator emits bounded, non-secret diagnostics.
        parser.exit(1, f"aurora: Invalid application configuration: {exc}\n")

    host = args.host if args.host is not None else defaults["host"]
    port = args.port if args.port is not None else defaults["port"]
    reload = args.reload if args.reload is not None else defaults["reload"]

    if not 1 <= port <= 65535:
        parser.error("--port must be between 1 and 65535")

    try:
        import uvicorn
    except ImportError:
        parser.exit(1, "aurora: Uvicorn is required to run the development server. Install with: pip install 'aurora-mvc[dev]'\n")

    # Console-script launchers do not necessarily include the working directory
    # in sys.path. Uvicorn must be able to import app:app from the project.
    cwd = str(Path.cwd().resolve())

    if cwd in sys.path:
        sys.path.remove(cwd)

    sys.path.insert(0, cwd)

    # Check simple modules consistently with and without reload. Do not import
    # developer code during preflight; dotted modules remain Uvicorn-owned.
    if local_project:
        module_file = Path.cwd().joinpath(*segments).with_suffix(".py")
        package_file = Path.cwd().joinpath(*segments, "__init__.py")
        if not module_file.is_file() and not package_file.is_file():
            parser.exit(1, f"aurora: Cannot find project application module '{module_name}'. "
                           "Check the target's spelling and generated project files.\n")

    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_name):
        # Preflight must check the requested filesystem location, not Python's
        # sys.modules cache (which may contain an unrelated app from elsewhere).
        local = ((Path.cwd() / f"{module_name}.py").is_file()
                 or (Path.cwd() / module_name / "__init__.py").is_file())
        # The conventional default specifically means the current project's
        # app, never an unrelated installed distribution named `app`.
        external = (module_name != "app" and
                    PathFinder.find_spec(module_name, [entry for entry in sys.path
                                                       if entry not in ("", cwd)]) is not None)
        if not local and not external:
            parser.exit(
                1,
                f"aurora: Cannot find application module '{module_name}'. "
                f"Check the target '{args.application}' or run from your project directory.\n",
            )

    uvicorn.run(args.application, host=host, port=port, reload=reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
