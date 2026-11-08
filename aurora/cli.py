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
)


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
    new.add_argument("name")
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
    app_actions.add_parser("add", help="Add a modular application").add_argument("name")
    app_actions.add_parser("list", help="List modular applications")
    app_actions.add_parser("show", help="Inspect a modular application").add_argument("name")

    for resource in ("controller", "view", "model"):
        group = sub.add_parser(resource, help=f"Add, list, or inspect {resource} components")
        actions = group.add_subparsers(dest="action", required=True)

        for verb in ("add", "list", "show"):
            command = actions.add_parser(verb, help=f"{verb.title()} {resource} components")
            command.add_argument("first", nargs="?" if verb == "list" else None,
                                 help="Component name, or legacy app name")

            if verb != "list":
                command.add_argument("second", nargs="?", help="Legacy component name")

            command.add_argument("--app", dest="app_option", help="Target modular app")

        if resource == "model":
            register = actions.add_parser("register", help="Register an explicit model reference in the project manifest")
            register.add_argument("reference", metavar="MODULE:CLASS")
            register.add_argument("--database", default="default", help="Database alias (default: default)")
            register.add_argument("--models-config", default="aurora_models.json", help="Model manifest JSON path")
            registered = actions.add_parser("registered", help="List model references registered for one database")
            registered.add_argument("--database", default="default", help="Database alias (default: default)")
            registered.add_argument("--models-config", default="aurora_models.json", help="Model manifest JSON path")

    # Database commands use explicit safety gates; planning remains read-only.
    db_group = sub.add_parser("db", help="Inspect databases and run guarded AuroraSQL operations")
    db_group.description = "Database commands: init, plan, migrate, backup, restore, history."
    db_group.epilog = "Normal workflow: aurora db init; aurora db plan; aurora db migrate; aurora db history"
    db_actions = db_group.add_subparsers(dest="action", required=True)

    for verb in ("plan", "history"):
        command = db_actions.add_parser(verb, help=(
            "Read applied migration history" if verb == "history"
            else "Inspect schema, plan changes, and report safety warnings (read-only)"))
        command.add_argument("--config", default="aurora_databases.json")
        command.add_argument("--database", default="default")

        if verb == "plan":
            command.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS", help=argparse.SUPPRESS)
            command.add_argument("--models-config", default="aurora_models.json")
            command.add_argument("--explain", action="store_true")
            command.add_argument("--format", choices=("text", "json"), default="text")
            command.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
            command.add_argument("--review-before", help="Existing before schema snapshot JSON")
            command.add_argument("--review-after", help="Existing after schema snapshot JSON")
            command.add_argument("--review-id", help="Migration review identifier")
            command.add_argument("--review-out", help="New non-executable review JSON destination")
            command.add_argument("--review-ack", help="Exact optional reviewed SHA-256 acknowledgement")
        else:
            command.add_argument("--history-file", help="Optional legacy history SQLite file")
            command.add_argument("--intents", action="store_true", help="Read guarded SQLite intent journal v2 instead of earlier history")
            command.add_argument("--verify-models", action="store_true", help="Verify supported migration history against discovered models and physical schema")
            command.add_argument("--models-config", default="aurora_models.json")
            command.add_argument("--format", choices=("text", "json"), default="text")

    for verb in ("backup", "restore"):
        command = db_actions.add_parser(verb, help=("Create a verified supported database backup" if verb == "backup" else "Inspect recovery options (restore execution unavailable)"))
        command.add_argument("--config", default="aurora_databases.json")
        command.add_argument("--database", default="default")

        if verb == "backup":
            command.add_argument("--to", help="New backup destination; never overwrites")
            command.add_argument("--login-path", help="MySQL client login-path profile (required for MySQL backup)")
        else:
            command.add_argument("--from", dest="source", required=True, help="Backup source path (restore execution currently unavailable)")
            command.add_argument("--check-only", action="store_true", help="Reserved for future guarded recovery; restore currently unavailable")
            command.add_argument("--login-path", help="Reserved legacy option; restore currently unavailable")
            command.add_argument("--staging-database", help="Reserved for future recovery; restore currently unavailable")
            command.add_argument("--staging-login-path", help="Reserved for future recovery; restore currently unavailable")
            command.add_argument("--staging-only", action="store_true", help="Reserved for future recovery; restore currently unavailable")

    init = db_actions.add_parser(
        "init",
        help="Create selected tables in an empty supported database (writes once)",
        description=(
            "Initialize an empty supported database from discovered Aurora models. "
            "If every selected table exists, report already initialized without modifying the database. "
            "Refuse partial or unrelated schemas; this command never migrates tables."
        ),
        epilog=(
            "Examples:\n"
            "  aurora db init\n"
            "  aurora db init --title 'Initial CMS database'\n"
            "  aurora db init --database accounting"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init.add_argument("--approve", help="Exact reviewed AuroraSQL create approval digest")
    init.add_argument("--confirm", help="Must be APPLY for guarded AuroraSQL initialization")
    init.add_argument("--title", help="Human-readable initial migration title")
    init.add_argument("--check", action="store_true",
                      help="AuroraSQL read-only initialization preflight; no database writes")
    init.add_argument("--config", default="aurora_databases.json", help="Database configuration JSON path")
    init.add_argument("--database", default="default", help="Configured database alias")
    init.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS", help=argparse.SUPPRESS)
    init.add_argument("--models-config", default="aurora_models.json", help="Explicit model manifest JSON path")
    sync = db_actions.add_parser('migrate', help='Review and safely apply supported database schema changes')
    sync.add_argument('--config', default='aurora_databases.json', help='Database connection configuration JSON path')
    sync.add_argument('-c', '--changes', help='Validated single-table schema-operation JSON (guarded SQLite execution)')
    sync.add_argument('--database', default='default', help='Configured database alias (default: default)')
    sync.add_argument('--model', action='append', metavar='MODULE:CLASS', help=argparse.SUPPRESS)
    sync.add_argument('--models-config', default='aurora_models.json', help='Explicit project model manifest JSON path')
    # Retain legacy preview flags for existing callers; the canonical
    # inspection and CI workflow is now `aurora db plan`.
    sync.add_argument('--explain', action='store_true', help=argparse.SUPPRESS)
    sync.add_argument('--format', choices=('text', 'json'), default='text', help=argparse.SUPPRESS)
    sync.add_argument('--dry-run', action='store_true', help=argparse.SUPPRESS)
    sync.add_argument('--check', action='store_true', help=argparse.SUPPRESS)
    sync.add_argument('--approve', help='Reviewed SHA-256 digest; APPLY still required')
    sync.add_argument('--review-file', help='Existing, validated AuroraSQL migration review JSON')
    sync.add_argument('--name', help='New migration review filename stem (required to apply)')
    sync.add_argument('--output-dir', default='migrations', help='Existing review directory (default: migrations)')
    sync.add_argument('--backup', help='New backup filename in an existing directory (required to apply)')
    sync.add_argument('--login-path', help=argparse.SUPPRESS)
    sync.add_argument('--schema', help='Exact MySQL schema (required for MySQL)')
    sync.add_argument('--table', help='One MySQL target table')
    sync.add_argument('--column', help='One nullable MySQL column; omit for CREATE TABLE')
    sync.add_argument('--apply', action='store_true', help='Explicit MySQL DDL execution only')
    sync.add_argument('--approve-review', help='Exact MySQL review SHA-256')
    sync.add_argument('--approve-sql', help='Exact MySQL SQL SHA-256')
    sync.add_argument('--confirm', help='Must be APPLY for MySQL execution')
    sync.add_argument('--title', help='Human-readable migration title (default: Untitled)')

    # Keep backend-specific limitations visible without implying universal support.
    sync.description = (
        "Use 'aurora db plan' to inspect changes and assess safety. "
        "This command reviews and applies only supported migrations after "
        "the required approval, backup and backend-specific checks. "
        "MySQL/PostgreSQL support repeated guarded, reviewed successors "
        "on verified migration history; unsupported, ambiguous and "
        "destructive changes are refused. Server JSON schema operations "
        "are not executable."
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
        "init", "plan", "migrate"
    ):
        project_dir = str(Path.cwd().resolve())

        if project_dir not in sys.path:
            sys.path.insert(0, project_dir)

    if args.command == "db" and args.action in ("backup", "restore") and args.backend == "aurorasql":
        if args.action == "restore":
            parser.exit(1, "aurora: AuroraSQL restore remains disabled pending recovery acceptance; refusing legacy fallback.\n")

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
        except Exception:
            parser.exit(1, "aurora: AuroraSQL backup refused; refusing legacy fallback; inspect source and destination manually.\n")

    if args.command == "db" and args.action == "migrate" and args.backend == "aurorasql":
        if args.title is not None and args.dry_run:
            parser.exit(1, "aurora: --title is for migration execution only.\n")
        # Validate read-only CLI contracts before attempting configuration or
        # model imports. Never interpret a write flag as preview permission.
        if args.check and not args.dry_run:
            parser.exit(1, "aurora: --check requires --dry-run.\n")

        if args.explain and not args.dry_run:
            parser.exit(1, "aurora: --explain requires --dry-run.\n")

        if args.format == "json" and not args.dry_run:
            parser.exit(1, "aurora: --format json requires --dry-run.\n")

        if args.dry_run and args.backup is not None:
            parser.exit(1, "aurora: --backup cannot be combined with --dry-run.\n")

        if args.changes is not None:
            from aurora.database.cli_explicit_operations import run_explicit_operations

            try:
                return run_explicit_operations(args)
            except Exception:
                parser.exit(1, "aurora: Explicit schema operation refused; inspect JSON, alias and schema; no database changes made.\n")

        if args.review_file:
            from aurora.database.cli_migrate_reviewed import run_reviewed_migration

            try:
                return run_reviewed_migration(args)
            except Exception:
                parser.exit(1, "aurora: Reviewed AuroraSQL migration refused; no legacy fallback; inspect review, models, journal and database state.\n")

        if args.apply or not args.dry_run:
            from aurora.database.cli_model_migration import run_model_migration

            try:
                return run_model_migration(args)
            except Exception:
                parser.exit(1, "aurora: Model-driven migration refused; inspect approval, backup, models and native history.\n")

        if args.approve or args.approve_review or args.approve_sql or args.confirm:
            parser.exit(1, "aurora: Use --dry-run for preview or --apply with exact approval, confirmation and backup; refusing legacy fallback.\n")

        if not args.explain and not args.check and args.backup is None:
            from aurora.database.cli_model_migration import run_model_migration

            try:
                return run_model_migration(args)
            except (RuntimeError, ValueError):
                # An ordinary plan remains available when no eligible additive
                # review exists. Never convert a failed write into this path.
                pass

        from types import SimpleNamespace
        from aurora.database.cli_plan import run_plan
        plan_args = SimpleNamespace(config=args.config, database=args.database,
                                    model=args.model or [], models_config=args.models_config,
                                    format=args.format, check=args.check)

        try:
            return run_plan(plan_args)
        except Exception:
            parser.exit(1, "aurora: AuroraSQL migration preview refused; inspect configuration and schema manually.\n")

    if args.command == "db" and args.action == "plan" and args.backend == "aurorasql":
        review_values = (args.review_before, args.review_after, args.review_id, args.review_out)

        if any(value is not None for value in review_values) or args.review_ack is not None:
            if args.check or args.explain or args.model or args.format != "text":
                parser.exit(1, "aurora: Review export cannot be combined with schema planning flags.\n")

            from aurora.database.cli_review_workflow import run_review_export

            try:
                return run_review_export(args)
            except Exception:
                parser.exit(1, "aurora: Read-only migration review refused; inspect snapshots, journal and destination.\n")

        from aurora.database.cli_plan import run_plan

        try:
            return run_plan(args)
        except (ValueError, TypeError, OSError, ImportError, AttributeError) as exc:
            # Do not expose database URLs, passwords, or driver error details.
            parser.exit(1, f"aurora: AuroraSQL plan refused: {type(exc).__name__}; check configuration, model references, and database access.\n")
        except Exception:
            parser.exit(1, "aurora: AuroraSQL plan failed; inspect database access and schema manually.\n")

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
        except Exception:
            parser.exit(1, "aurora: AuroraSQL initialization preflight refused; inspect configuration and schema manually.\n")

    if args.command == "model" and args.action in ("register", "registered"):
        from aurora.database.cli_registration import register_model, registered_models

        try:
            if args.action == "register":
                if os.environ.get("AURORA_ENV", "development").lower() == "production":
                    raise ValueError("Refusing to modify model manifest in production environment")

                changed = register_model(args.reference, database=args.database, manifest=args.models_config)
                print(("Registered " if changed else "Already registered: ") + args.reference + f" (database: {args.database})")
            else:
                refs = registered_models(database=args.database, manifest=args.models_config)
                print("\n".join(refs) if refs else f"No models registered for database: {args.database}")
        except (ValueError, OSError) as exc:
            parser.exit(1, f"aurora: {exc}\n")

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

        if args.action == "add" and os.environ.get("AURORA_ENV", "development").lower() == "production":
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
