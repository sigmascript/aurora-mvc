"""Minimal, explicit Aurora developer CLI."""
from __future__ import annotations

import argparse
from importlib.util import find_spec
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
import re
import sys

from ._cli_scaffolding import (
    create_project,
    add_app,
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
)


def _database_failure(exc, args, action):
    """Explain project-model failures without leaking database URLs or credentials.

    Only a traceback frame within the configured project is eligible for a
    source location. Database/driver exception text is deliberately hidden.
    """
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

    if isinstance(exc, (ValueError, PermissionError)):
        # Do not echo arbitrary exception text: URLs and credentials may be embedded.
        return (f"aurora: Database {action} refused ({type(exc).__name__}). "
                "Check model declarations, database settings, and supported operations.\n")
    return (f"aurora: Database {action} failed ({type(exc).__name__}). "
            "Check your database connection and model definitions. No automatic retry was attempted.\n")


def main(argv: list[str] | None = None) -> int:
    """Run the Aurora CLI and return its process exit status.

        Public command names and legacy aliases are preserved for compatibility."""
    # Build the public command tree while retaining hidden legacy aliases.
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
    new.add_argument("name", metavar="project", help="New Python-compatible project directory name")
    new.add_argument("--mvc", action="store_true", help="Create a minimal MVC project")
    new.add_argument("--modular", action="store_true", help="Create an MVC project with an apps/ directory")

    # Legacy commands remain callable by existing projects and automation.
    for command in ("create-app", "add-app"):
        add = sub.add_parser(command, help=argparse.SUPPRESS)
        add.add_argument("name")

    sub.add_parser("list-apps", help=argparse.SUPPRESS)
    show = sub.add_parser("show-app", help=argparse.SUPPRESS)
    show.add_argument("name")

    for command in ("create-controller", "create-view"):
        scaffold = sub.add_parser(command, help=argparse.SUPPRESS)
        scaffold.add_argument("app", help="Existing modular app name")
        scaffold.add_argument("name", help="Controller class name or view name")

    # Keep legacy command dispatch for compatibility, but omit it from public help.
    sub._choices_actions[:] = [choice for choice in sub._choices_actions
                               if choice.help != argparse.SUPPRESS]

    # Canonical resource-first commands. Existing flat commands remain supported.
    app_group = sub.add_parser("app", help="Add, list, or inspect modular applications")
    app_actions = app_group.add_subparsers(dest="action", required=True)
    app_actions.add_parser("add", help="Add a modular application").add_argument("name", metavar="app", help="New modular application name")
    app_actions.add_parser("list", help="List modular applications")
    app_actions.add_parser("show", help="Inspect a modular application").add_argument("name", metavar="app", help="Existing modular application name")

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

        if resource == "model":
            # Existing alpha scripts may still use these until the dedicated
            # database CLI transition migrates their callers. They are not
            # part of the new public model lifecycle.
            register = actions.add_parser("register", help=argparse.SUPPRESS)
            register.add_argument("reference")
            register.add_argument("--database", default="default")
            register.add_argument("--models-config", default="aurora_models.json")
            registered = actions.add_parser("registered", help=argparse.SUPPRESS)
            registered.add_argument("--database", default="default")
            registered.add_argument("--models-config", default="aurora_models.json")

    # Database commands use explicit safety gates; planning remains read-only.
    db_group = sub.add_parser("db", help="Check and manage your project databases")
    db_group.description = "Database commands: check, init, migrate, backup, restore, history."
    db_group.epilog = "Normal workflow: aurora db check; aurora db init; aurora db migrate; aurora db history"
    db_actions = db_group.add_subparsers(
        dest="action", required=True,
        metavar="{check,init,migrate,backup,restore,history}",
    )

    # Ordinary database inspection uses project conventions, not caller-supplied
    # model manifests or configuration files. The historical `plan` parser
    # remains isolated for existing review/automation callers.
    check = db_actions.add_parser(
        "check", help="Check database setup and model changes without making changes",
    )
    check.add_argument("--database", default="default", help="Database name from your project settings")
    check.set_defaults(config="aurora_databases.json", models_config="aurora_models.json",
                       model=[], format="text", check=False, explain=False)

    plan = db_actions.add_parser("plan", help=argparse.SUPPRESS)
    plan.add_argument("--config", default="aurora_databases.json")
    plan.add_argument("--database", default="default")
    plan.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS", help=argparse.SUPPRESS)
    plan.add_argument("--models-config", default="aurora_models.json")
    plan.add_argument("--explain", action="store_true", help=argparse.SUPPRESS)
    plan.add_argument("--format", choices=("text", "json"), default="text")
    plan.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    plan.add_argument("--review-before")
    plan.add_argument("--review-after")
    plan.add_argument("--review-id")
    plan.add_argument("--review-out")
    plan.add_argument("--review-ack")

    history = db_actions.add_parser("history", help="Show recorded database changes")
    history.add_argument("--database", default="default",
                         help="Database name from your project settings")
    history.set_defaults(config="aurora_databases.json", models_config="aurora_models.json",
                         history_file=None, intents=False, verify_models=False, format="text")

    # Keep the historical alias parseable without making it a seventh public
    # command. The documented commands are the six shown in the metavar.
    db_actions._choices_actions = [choice for choice in db_actions._choices_actions
                                   if choice.dest != "plan"]

    backup = db_actions.add_parser(
        "backup", help="Create a verified database backup without overwriting files",
        description=("Back up the selected database to a new file. Existing backups "
                     "are never overwritten. MySQL on Windows uses a configured login-path."),
    )
    backup.add_argument("--database", default="default", help="Database name from project settings")
    backup.add_argument("--to", required=True, help="New backup file path")
    backup.add_argument("--login-path", help="MySQL authentication profile (Windows)")
    backup.set_defaults(config="aurora_databases.json")

    restore = db_actions.add_parser(
        "restore", help="Restore a verified SQLite backup into an absent database",
        description=("Restore a verified SQLite backup to a database file that does not exist. "
                     "Existing databases are never overwritten. "
                     "The matching migration snapshots must already be present."),
    )
    restore.add_argument("--database", default="default", help="Database name from project settings")
    restore.add_argument("--from", dest="source", required=True, help="Backup file path")
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
    init.add_argument("--database", default="default", help="Database name from your project settings")
    init.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS", help=argparse.SUPPRESS)
    init.set_defaults(models_config="aurora_models.json")
    sync = db_actions.add_parser('migrate', help='Apply supported model and schema changes')
    sync.add_argument('-c', '--change', dest='changes',
                      help='JSON file describing specific schema changes')
    sync.add_argument('--database', default='default', help='Database name from your project settings')
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
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--no-reload", action="store_true", help="Disable automatic reload (enabled by default)")
    args = parser.parse_args(argv)

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
        "init", "check", "plan", "migrate"
    ):
        project_dir = str(Path.cwd().resolve())

        if project_dir not in sys.path:
            sys.path.insert(0, project_dir)

    if args.command == "db" and args.action in ("backup", "restore") and args.backend == "aurorasql":
        if args.action == "restore":
            from aurora.database.cli_plan import _load_config
            from urllib.parse import urlsplit
            try:
                config, _ = _load_config(args.config)
                if urlsplit(config.resolve(args.database)).scheme not in ("sqlite", "sqlite+pysqlite"):
                    raise ValueError("Server database restore is not available")
                from aurora.database.cli_empty_restore import restore_empty_sqlite
                return restore_empty_sqlite(args.config, args.database, args.source)
            except FileExistsError:
                parser.exit(1, "aurora: Target database already exists; restore will not overwrite it.\n")
            except Exception as exc:
                # Do not echo raw connection URLs or filesystem-backed driver errors.
                message = str(exc)
                safe = ("Restore supports only a configured SQLite file", "In-memory databases cannot be restored",
                        "Invalid SQLite database path", "Restore source and destination must be different",
                        "Target directory must already exist", "Backup must be an existing regular SQLite file",
                        "Database has SQLite journal/WAL files; restore refused",
                        "Backup failed SQLite integrity verification", "Backup contains foreign-key violations",
                        "Restore in production requires separate approval")
                if message not in safe:
                    message = "Restore refused: verify the backup, migration snapshots, and empty destination."
                parser.exit(1, f"aurora: {message}\n")

        from aurora.database.cli_plan import _load_config
        from urllib.parse import urlsplit

        try:
            config, _ = _load_config(args.config)
            scheme = urlsplit(config.resolve(args.database)).scheme

            if scheme == "mysql+pymysql":
                if os.environ.get("AURORA_ENV", "development").lower() == "production":
                    raise ValueError("Production backup requires separate approval")

                from aurora.database.cli_mysql_backup import backup_mysql_database
                backup_mysql_database(args.config, args.database, args.to, args.login_path)
                return 0

            if scheme == "postgresql+psycopg":
                if os.environ.get("AURORA_ENV", "development").lower() == "production":
                    raise ValueError("Production backup requires separate approval")

                if args.login_path:
                    raise ValueError("--login-path is specific to MySQL")

                from aurora.database.cli_postgres_backup import backup_postgres_database
                backup_postgres_database(args.config, args.database, args.to)
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

        if args.changes is not None:
            from aurora.database.cli_explicit_operations import run_explicit_operations

            try:
                return run_explicit_operations(args)
            except Exception:
                parser.exit(1, "aurora: Explicit schema operation refused; inspect JSON, alias and schema.\n")

        from aurora.database.cli_model_migration import run_model_migration

        try:
            return run_model_migration(args)
        except Exception as exc:
            diagnostic = _database_failure(exc, args, "migration")
            parser.exit(1, "aurora: Model-driven migration refused. " + diagnostic.removeprefix("aurora: "))

    if args.command == "db" and args.action in ("check", "plan") and args.backend == "aurorasql":
        review_values = (getattr(args, "review_before", None), getattr(args, "review_after", None),
                         getattr(args, "review_id", None), getattr(args, "review_out", None))

        if any(value is not None for value in review_values) or getattr(args, "review_ack", None) is not None:
            if args.check or args.explain or args.model or args.format != "text":
                parser.exit(1, "aurora: Review export cannot be combined with schema planning flags.\n")

            from aurora.database.cli_review_workflow import run_review_export

            try:
                return run_review_export(args)
            except Exception:
                parser.exit(1, "aurora: Read-only migration review refused; inspect snapshots, journal and destination.\n")

        from aurora.database.cli_plan import run_plan

        try:
            if args.action == "check":
                from aurora.database.cli_plan import run_check
                return run_check(args)

            return run_plan(args)
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
        except Exception:
            # Never print URLs, driver errors, or connection credentials.
            parser.exit(1, "aurora: AuroraSQL history refused; inspect database configuration and journal manually.\n")

    if args.command == "db" and args.action == "init" and args.backend == "aurorasql":
        from aurora.database.cli_init import run_init_check, run_init_apply

        try:
            if args.check:
                if args.approve or args.confirm or args.title is not None:
                    parser.exit(1, "aurora: --check cannot include execution options or a title.\n")

                return run_init_check(args)

            if os.environ.get("AURORA_ENV", "development").lower() == "production":
                parser.exit(1, "aurora: Refusing AuroraSQL initialization in production.\n")

            return run_init_apply(args)
        except ValueError as exc:
            if str(exc) == "At least one model is required":
                parser.exit(1, "aurora: No models found. Add a model with 'aurora model add NAME'.\n")
            parser.exit(1, _database_failure(exc, args, "initialization"))
        except Exception as exc:
            parser.exit(1, _database_failure(exc, args, "initialization"))

    if args.command == "model" and args.action in ("register", "registered"):
        from aurora.database.cli_registration import register_model, registered_models

        try:
            if args.action == "register":
                if os.environ.get("AURORA_ENV", "development").lower() == "production":
                    raise ValueError("Refusing model manifest edits in production")

                changed = register_model(args.reference, database=args.database,
                                         manifest=args.models_config)
                print(("Registered " if changed else "Already registered: ") + args.reference)
            else:
                refs = registered_models(database=args.database, manifest=args.models_config)
                print("\n".join(refs) if refs else "No models registered.")
        except (ValueError, OSError) as error:
            parser.exit(1, f"aurora: {error}\n")

        return 0

    # Normalize grouped commands to the established, tested handlers.
    # No duplication of project modification logic.
    if args.command == "app":
        args.command = {"add": "create-app", "list": "list-apps", "show": "show-app"}[args.action]
    elif args.command in ("controller", "view", "model"):
        resource = args.command

        if getattr(args, "second", None) is not None:
            if args.app_option is not None:
                parser.error("Specify the app either positionally or with --app, not both. Example: aurora controller add User --app blog")

            app_name, name = args.first, args.second
        else:
            app_name, name = args.app_option, args.first

            if args.action == "list" and args.first is not None and args.app_option is None:
                app_name, name = args.first, None  # Legacy: resource list APP

        if args.action != "list" and not name:
            parser.error(f"{resource} {args.action} requires a component name. Example: aurora {resource} {args.action} "
                         + ("user" if resource == "view" else "User"))

        if args.action in ("add", "remove") and os.environ.get("AURORA_ENV", "development").lower() == "production":
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")

        try:
            if args.action == "add":
                if resource == "model":
                    backend = args.backend
                    destination = (create_shared_component("model", name, backend=backend)
                                   if app_name is None else create_model(app_name, name, backend=backend))
                elif app_name is None:
                    destination = (create_standalone_controller(name) if resource == "controller"
                                   else create_shared_component(resource, name))
                else:
                    destination = {"controller": create_controller, "view": create_view}[resource](app_name, name)

                note = {"controller": " (route registered)", "view": " (routes unchanged)",
                        "model": " (database unchanged; import model explicitly)"}[resource]

                if resource == "model" and backend == "aurorasql":
                    from aurora.database.cli_registration import register_generated_model
                    reference, _ = register_generated_model(destination, name)
                    note = f" (registered {reference}; database unchanged)"

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

    if args.command in ("create-app", "add-app"):
        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")

        try:
            destination = add_app(args.name)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print(f"Created {destination}\nRoute: /{args.name}")
        return 0

    if args.command in ("create-controller", "create-view", "create-model"):
        if os.environ.get("AURORA_ENV", "development").lower() == "production":
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")

        try:
            action = {"create-controller": create_controller, "create-view": create_view,
                      "create-model": create_model}[args.command]
            destination = action(args.app, args.name)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        suffix = {"create-controller": " (route registered)",
                  "create-view": " (routes unchanged)",
                  "create-model": " (database unchanged; import model explicitly)"}[args.command]
        print(f"Created {destination}" + suffix)
        return 0

    if args.command == "list-apps":
        try:
            names = list_apps()
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print("\n".join(names) if names else "No apps found.")
        return 0

    if args.command == "show-app":
        try:
            info = show_app(args.name)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

        print(f"App: {info['name']}\nPath: {info['path']}")
        print("Controllers: " + (", ".join(info["controllers"]) or "(none)"))
        print("Views: " + (", ".join(info["views"]) or "(none)"))
        return 0

    if not 1 <= args.port <= 65535:
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
    module_name = args.application.partition(":")[0]

    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_name):
        try:
            module_spec = find_spec(module_name)
        except (ImportError, ValueError, AttributeError):
            module_spec = None

        if module_spec is None:
            parser.exit(
                1,
                f"aurora: Cannot find application module '{module_name}'. "
                f"Check the target '{args.application}' or run from your project directory.\n",
            )

    uvicorn.run(args.application, host=args.host, port=args.port, reload=not args.no_reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
