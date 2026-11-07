"""Project and component scaffolding for the Aurora developer CLI.

Generators remain available through ``aurora.cli`` for compatibility.
"""
from __future__ import annotations

import ast
import json
import keyword
import os
from pathlib import Path
import re
import tempfile

from ._cli_templates import (
    _PROJECT, _MVC_APP, _MVC_HOME, _MVC_LAYOUT, _MVC_VIEW,
    _MODULAR_APP, _MODULAR_HOME, _MODULAR_BLOG, _MODULAR_LAYOUT,
    _MODULAR_HOME_VIEW, _MODULAR_BLOG_VIEW, _MVC_COMPONENTS,
    _MODULAR_NAVIGATION, _MODULAR_COMPONENTS, _MVC_CSS, _MODULAR_CSS,
)


def create_project(name: str, *, directory: Path | None = None, mvc: bool = False, modular: bool = False) -> Path:
    """Create a small importable Aurora project; never overwrite files."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or keyword.iskeyword(name) or name in {"aurora", "tests"}:
        raise ValueError("Project name must be a valid Python identifier (not 'aurora' or 'tests').")

    if modular:
        mvc = True

    root = (directory or Path.cwd()) / name

    if root.exists():
        raise FileExistsError(f"Destination already exists: {root}")

    root.mkdir(parents=False)

    try:
        (root / "__init__.py").write_text('"""Aurora project."""\n', encoding="utf-8")
        app_source = _MODULAR_APP if modular else (_MVC_APP if mvc else _PROJECT)
        (root / "app.py").write_text(app_source.format(name=name), encoding="utf-8")

        if modular:
            for relative in (
                "apps", "apps/home", "apps/home/controllers", "apps/home/views",
                "apps/blog", "apps/blog/controllers",
                "apps/blog/views", "views", "statics", "models",
            ):
                folder = root / relative
                folder.mkdir()

                if relative in {"apps", "apps/home", "apps/home/controllers", "apps/blog", "apps/blog/controllers", "models"}:
                    (folder / "__init__.py").write_text("", encoding="utf-8")

            (root / "apps/home/controllers/Home.py").write_text(
                _MODULAR_HOME.format(name=name), encoding="utf-8"
            )
            (root / "apps/blog/controllers/Blog.py").write_text(
                _MODULAR_BLOG, encoding="utf-8"
            )
            (root / "views/layout.html").write_text(_MODULAR_LAYOUT.format(), encoding="utf-8")
            (root / "views/navigation.html").write_text(_MODULAR_NAVIGATION, encoding="utf-8")
            (root / "views/components.html").write_text(_MODULAR_COMPONENTS, encoding="utf-8")
            (root / "apps/home/views/index.html").write_text(_MODULAR_HOME_VIEW.format(), encoding="utf-8")
            (root / "apps/blog/views/index.html").write_text(_MODULAR_BLOG_VIEW.format(), encoding="utf-8")
        elif mvc:
            (root / "models").mkdir()
            (root / "models" / "__init__.py").write_text("", encoding="utf-8")
            (root / "controllers").mkdir()
            (root / "controllers" / "__init__.py").write_text("", encoding="utf-8")
            (root / "controllers" / "Home.py").write_text(
                _MVC_HOME.format(name=name), encoding="utf-8"
            )
            (root / "views").mkdir()
            (root / "statics").mkdir()
            (root / "views" / "components").mkdir()
            (root / "views" / "components" / "actions.html").write_text(_MVC_COMPONENTS, encoding="utf-8")
            (root / "views" / "layout.html").write_text(_MVC_LAYOUT.format(), encoding="utf-8")
            (root / "views" / "home.html").write_text(_MVC_VIEW.format(), encoding="utf-8")

        if mvc:
            css = root / "statics" / "css"
            css.mkdir()
            (css / "site.css").write_text(
                (_MVC_CSS if not modular else _MODULAR_CSS),
                encoding="utf-8",
            )
        # Explicit, inert database starter files. Never open or create a database
        # during scaffolding; developers register models before planning migrations.
        (root / "aurora_databases.json").write_text(
            json.dumps({"databases": {"default": {"url": "sqlite:///data.sqlite3"}}}, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "aurora_config.json").write_text(
            json.dumps({
                "format": "aurora.application.v1",
                "application": {"name": name, "timezone": "UTC"},
                "limits": {},
                "constants": {},
            }, indent=2) + "\n", encoding="utf-8",
        )
        (root / "aurora_models.json").write_text(
            json.dumps({"databases": {}}, indent=2) + "\n", encoding="utf-8",
        )
        (root / "migrations").mkdir()
        (root / "backups").mkdir()
        (root / "README.md").write_text(
            f"# {name}\n\nRun from the parent directory: "
            f"`aurora run {name}.app:app`\n\n"
            "## Application settings\n\n"
            "- `aurora_config.json` holds validated, nonsecret application defaults.\n"
            "- Do not place user preferences or credentials in this file.\n"
            "- `app.py` loads it explicitly with `Aurora.from_config`.\n\n"
            "## Database setup\n\n"
            "- `aurora_databases.json` configures the `default` SQLite alias.\n"
            "- AuroraSQL discovers one CamelCase model per file from shared `models/` "
            "and modular `apps/*/models/` directories.\n"
            "- `aurora_models.json` optionally assigns models to named databases; "
            "unassigned discovered models use `default`.\n"
            "- No SQLite file or tables are created by scaffolding. Initialize "
            "your database explicitly before inspection or migrations.\n"
            "- `aurora db plan` previews changes without writes; `aurora db migrate` "
            "interactively reviews supported changes.\n"
            "- `migrations/` and `backups/` are prepared for reviewed operations.\n",
            encoding="utf-8",
        )
    except BaseException:
        # Do not leave a partial scaffold behind on write failure.
        import shutil
        shutil.rmtree(root)
        raise

    return root



def add_app(name: str, *, directory: Path | None = None) -> Path:
    """Add a concrete controller and view to a generated modular project.

    Updates only app.py files containing our explicit scaffold anchors.
    Existing applications and arbitrary user files are never overwritten.
    """

    if (not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name)
            or keyword.iskeyword(name) or name in {"aurora", "tests"}):
        raise ValueError("App name must be a valid Python identifier (not 'aurora' or 'tests').")

    root = (directory or Path.cwd()).resolve()
    app_file = root / "app.py"
    apps = root / "apps"

    if not app_file.is_file() or not apps.is_dir():
        raise ValueError("Run this command from a generated modular Aurora project directory.")

    destination = apps / name

    if any(child.name.casefold() == name.casefold() for child in apps.iterdir()):
        raise FileExistsError(f"App name already exists (case-insensitive): {name}")

    if destination.exists():
        raise FileExistsError(f"App already exists: {destination}")

    source = app_file.read_text(encoding="utf-8")
    anchors = ("# aurora:imports", "# aurora:views", "# aurora:routes")

    if any(source.count(anchor) != 1 for anchor in anchors):
        raise ValueError("This project's app.py has no scaffold insertion anchors; add the app manually.")
    # Keep generated routes predictable; do not silently introduce collisions.
    route = f"/{name}/"

    if re.search(r"app\.route\(\s*[\'\"]" + re.escape(route) + r"[\'\"]", source):
        raise ValueError(f"Route already registered: {route}")

    class_name = "".join(part.capitalize() for part in name.split("_"))
    updated = source.replace("# aurora:imports", f"from apps.{name}.controllers.{class_name} import {class_name}\n# aurora:imports")
    updated = updated.replace("        # aurora:views", f'        "{name}": ROOT / "apps" / "{name}" / "views",\n        # aurora:views')
    updated = updated.replace("# aurora:routes", f'app.route("/{name}/", {class_name})\n# aurora:routes')

    # Prepare the new app in a sibling staging directory. No developer file is
    # changed until all scaffold files have been written successfully.
    import shutil
    staging = Path(tempfile.mkdtemp(prefix=f".{name}-", dir=apps))
    published = False

    try:
        (staging / "__init__.py").write_text("", encoding="utf-8")
        controllers = staging / "controllers"
        controllers.mkdir()
        (controllers / "__init__.py").write_text("", encoding="utf-8")
        (controllers / f"{class_name}.py").write_text(
            f'"""{class_name} controller."""\nfrom aurora import Controller, View\n\n\n'
            f'class {class_name}(Controller):\n'
            '    def get(self):\n'
            f'        return View("{name}/index", title="{name.replace("_", " ").title()}")\n',
            encoding="utf-8",
        )
        models = staging / "models"
        models.mkdir()
        (models / "__init__.py").write_text("", encoding="utf-8")
        view_dir = staging / "views"
        view_dir.mkdir()
        (view_dir / "index.html").write_text(
            '{% extends "layout.html" %}\n'
            '{% from "components.html" import action_link %}\n\n'
            '{% block title %}{{ title }}{% endblock %}\n\n'
            '{% block content %}\n'
            '    <h1>{{ title }}</h1>\n'
            '    <p>Your new Aurora app is ready.</p>\n'
            '    {{ action_link("Back to home", "/") }}\n'
            '{% endblock %}\n', encoding="utf-8",
        )

        # Write the replacement first, then atomically swap app.py on the
        # same filesystem. A failure here leaves the original untouched.
        temp_app = None

        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                                             prefix=".aurora-app-", suffix=".tmp",
                                             delete=False) as stream:
                temp_app = Path(stream.name)
                stream.write(updated)
            # Publish the app before registering its route. If the final
            # replacement fails, remove only our newly published directory.
            staging.rename(destination)
            published = True
            os.replace(temp_app, app_file)
            temp_app = None
        finally:
            if temp_app is not None:
                temp_app.unlink(missing_ok=True)
    except BaseException:
        if published:
            shutil.rmtree(destination)

        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)

    return destination



def _modular_apps_directory(directory: Path | None = None) -> Path:
    """Locate an explicit modular project without importing developer code."""
    root = (directory or Path.cwd()).resolve()

    if not (root / "app.py").is_file() or not (root / "apps").is_dir():
        raise ValueError("Run this command from a modular Aurora project directory.")

    return root / "apps"


def list_apps(*, directory: Path | None = None) -> list[str]:
    """List app packages in a modular project; do not execute their code."""
    apps = _modular_apps_directory(directory)
    return sorted(p.name for p in apps.iterdir()
                  if p.is_dir() and not p.is_symlink()
                  and (p / "__init__.py").is_file()
                  and p.name.isidentifier() and not keyword.iskeyword(p.name))


def show_app(name: str, *, directory: Path | None = None) -> dict[str, object]:
    """Describe an app's local files without evaluating imports or routes."""

    if not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError("App name must be a valid Python identifier.")

    apps = _modular_apps_directory(directory)

    if name not in list_apps(directory=directory):
        raise ValueError(f"App not found: {name}")

    root = apps / name
    controllers = sorted(p.stem for p in (root / "controllers").glob("*.py")
                         if p.is_file() and p.name != "__init__.py") if (root / "controllers").is_dir() else []
    views = sorted(p.relative_to(root / "views").as_posix()
                   for p in (root / "views").rglob("*.html") if p.is_file()) if (root / "views").is_dir() else []
    return {"name": name, "path": str(root), "controllers": controllers, "views": views}




def _scaffold_app_root(app_name: str, directory: Path | None = None) -> Path:
    """Resolve an existing modular app without following app-directory symlinks."""

    if (
        not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", app_name)
        or keyword.iskeyword(app_name)
    ):
        raise ValueError("App name must be a lowercase Python identifier.")

    apps = _modular_apps_directory(directory)
    root = apps / app_name

    if root.is_symlink() or not root.is_dir() or not (root / "__init__.py").is_file():
        raise ValueError(f"Modular app not found: {app_name}")

    return root


def _create_scaffold_file(folder: Path, filename: str, content: str) -> Path:
    """Create a file exclusively; never overwrite existing developer content."""

    if folder.is_symlink() or not folder.is_dir():
        raise ValueError(f"Expected a real existing directory: {folder}")

    destination = folder / filename

    with destination.open("x", encoding="utf-8") as stream:
        stream.write(content)

    return destination



def _controller_slug(name: str) -> str:
    """Convert CamelCase to a readable URL segment."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name).lower()


def _existing_component(folder: Path, name: str, resource: str) -> Path | None:
    """Resolve exact or legacy filenames without importing user code."""

    if not folder.is_dir() or folder.is_symlink():
        return None

    candidates = {name.casefold()}

    if resource == "controller":
        candidates.add(name.lower().casefold())

    if resource == "model":
        candidates.add(re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower().casefold())

    matches = [item for item in folder.iterdir()
               if item.suffix == ".py" and item.name != "__init__.py"
               and item.stem.casefold() in candidates]

    if len(matches) > 1:
        raise ValueError(f"Ambiguous {resource} name: {name}")

    return matches[0] if matches else None


def _reject_component_collision(folder: Path, name: str, resource: str) -> None:
    existing = _existing_component(folder, name, resource)

    if existing is not None:
        raise FileExistsError(f"{resource.title()} already exists (existing file will not be overwritten): {existing}")


def _resolve_component(folder: Path, name: str, resource: str) -> Path:
    path = _existing_component(folder, name, resource)

    if path is None or path.is_symlink() or not path.is_file():
        raise ValueError(f"{resource.title()} not found: {name}")

    return path


def _controller_binding(source: str, app_name: str, name: str) -> str:
    """Use a plain name unless a top-level binding already occupies it."""
    tree = ast.parse(source)
    occupied = set()
    def target(node):
        if isinstance(node, ast.Name):
            occupied.add(node.id)
        elif isinstance(node, (ast.Tuple, ast.List)):
            for child in node.elts:
                target(child)
    def scan(nodes):
        for node in nodes:
            if isinstance(node, ast.Import):
                occupied.update(a.asname or a.name.split('.')[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if any(a.name == '*' for a in node.names):
                    raise ValueError('Cannot safely register alongside wildcard imports.')

                occupied.update(a.asname or a.name for a in node.names)
            elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                occupied.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    target(t)
            elif isinstance(node, ast.AnnAssign):
                target(node.target)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                target(node.target)
                scan(node.body + node.orelse)
            elif isinstance(node, (ast.If, ast.While)):
                scan(node.body + node.orelse)
            elif isinstance(node, (ast.Try, getattr(ast, "TryStar", ast.Try))):
                scan(node.body + node.orelse + node.finalbody)

                for handler in node.handlers:
                    if handler.name:
                        occupied.add(handler.name)

                    scan(handler.body)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars:
                        target(item.optional_vars)

                scan(node.body)
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func

                if isinstance(func, ast.Name) and func.id in ('exec', 'eval'):
                    raise ValueError('Dynamic execution prevents safe registration.')
    scan(tree.body)

    if name not in occupied:
        return name

    base = ''.join(word.capitalize() for word in app_name.split('_')) + name
    alias = base
    i = 2

    while alias in occupied:
        alias = f'{base}{i}'
        i += 1

    return alias

def create_controller(app_name: str, name: str, *,
                      directory: Path | None = None) -> Path:
    """Generate and register a controller in an anchored modular project."""

    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or keyword.iskeyword(name):
        raise ValueError(f"Invalid controller name {name!r}. Use CamelCase (e.g. User or AccountSettings).")

    root = _scaffold_app_root(app_name, directory)
    project = root.parent.parent
    app_file = project / "app.py"
    source = app_file.read_text(encoding="utf-8")
    anchors = ("# aurora:imports", "# aurora:routes")

    if any(source.count(anchor) != 1 for anchor in anchors):
        raise ValueError("app.py must have unique Aurora import and route anchors.")

    module = name
    destination = root / "controllers" / f"{module}.py"
    _reject_component_collision(root / "controllers", name, "controller")
    route = f"/{app_name}/{_controller_slug(name)}/"

    # Keep the existing app's base route untouched and avoid duplicate registrations.
    if re.search(r"app\.route\(\s*[\'\"]" + re.escape(route) + r"[\'\"]", source):
        raise ValueError(f"Route already registered: {route}")

    binding = _controller_binding(source, app_name, name)
    suffix = f" as {binding}" if binding != name else ""
    import_line = f"from apps.{app_name}.controllers.{module} import {name}{suffix}"

    if import_line in source:
        raise ValueError("Controller import already registered.")

    updated = source.replace("# aurora:imports", import_line + "\n# aurora:imports")
    updated = updated.replace("# aurora:routes",
                              f'app.route("{route}", {binding})\n# aurora:routes')
    content = (f'"""Controller {name} for app {app_name}."""\n'
               'from aurora import Controller\n\n\n'
               f'class {name}(Controller):\n'
               '    def get(self):\n'
               '        return "TODO: implement this controller"\n')
    # Write the new module, then atomically replace app.py. Roll back the new
    # module if app.py cannot be published. Never overwrite an existing module.
    temp_app = None
    created = False

    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=project,
                                         prefix=".aurora-controller-", suffix=".tmp",
                                         delete=False) as stream:
            temp_app = Path(stream.name)
            stream.write(updated)

        _create_scaffold_file(root / "controllers", f"{module}.py", content)
        created = True
        os.replace(temp_app, app_file)
        temp_app = None
    except BaseException:
        if created:
            destination.unlink()

        raise
    finally:
        if temp_app is not None:
            temp_app.unlink(missing_ok=True)

    return destination


def _standalone_controller_root(directory: Path | None = None) -> Path:
    """Locate a generated standalone MVC project without executing its code."""
    root = (directory or Path.cwd()).resolve()
    controllers = root / "controllers"

    if ((root / "app.py").is_symlink() or not (root / "app.py").is_file()
            or controllers.is_symlink() or not controllers.is_dir()
            or not (controllers / "__init__.py").is_file()
            or (root / "apps").exists()):
        raise ValueError("Cannot use standalone controllers here. Run this command inside a standalone MVC project created with: aurora new myapp --mvc")

    return root


def create_standalone_controller(name: str, *, directory: Path | None = None) -> Path:
    """Create and register a controller in a standalone MVC scaffold."""

    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or keyword.iskeyword(name):
        raise ValueError(f"Invalid controller name {name!r}. Use CamelCase (e.g. User or AccountSettings).")

    root = _standalone_controller_root(directory)
    app_file = root / "app.py"
    source = app_file.read_text(encoding="utf-8")

    # The original standalone scaffold has no anchors. Require a recognizable
    # Aurora application before appending the registration.
    if not re.search(r"(?m)^app\s*=\s*Aurora(?:\.from_config)?\(", source):
        raise ValueError("Cannot safely register controller: app.py has no standard Aurora app assignment.")

    module = name
    destination = root / "controllers" / f"{module}.py"
    _reject_component_collision(root / "controllers", name, "controller")
    route = f"/{_controller_slug(name)}/"

    if re.search(r"app\.route\(\s*['\"]" + re.escape(route) + r"['\"]", source):
        raise ValueError(f"Route already registered: {route}")

    binding = _controller_binding(source, "", name)
    suffix = f" as {binding}" if binding != name else ""
    import_line = f"from controllers.{module} import {name}{suffix}"

    if import_line in source:
        raise ValueError("Controller import already registered.")
    # Insert the import before the first top-level app assignment, so the
    # controller is available when its route is registered.
    tree = ast.parse(source)
    app_assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "app" for t in node.targets)]

    if len(app_assignments) != 1:
        raise ValueError("Cannot safely register controller: expected one top-level app assignment.")

    lines = source.splitlines(keepends=True)
    lines.insert(app_assignments[0].lineno - 1, import_line + "\n")
    updated = "".join(lines).rstrip("\n") + f'\napp.route("{route}", {binding})\n'
    ast.parse(updated)
    content = (f'"""{name} page controller."""\n'
               'from aurora import Controller\n\n\n'
               f'class {name}(Controller):\n'
               '    def get(self):\n'
               '        return "TODO: implement this controller"\n')
    temp_app = None
    created = False

    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                                         prefix=".aurora-controller-", suffix=".tmp",
                                         delete=False) as stream:
            temp_app = Path(stream.name)
            stream.write(updated)

        _create_scaffold_file(root / "controllers", f"{module}.py", content)
        created = True
        os.replace(temp_app, app_file)
        temp_app = None
    except BaseException:
        if created:
            destination.unlink()

        raise
    finally:
        if temp_app is not None:
            temp_app.unlink(missing_ok=True)

    return destination


def list_standalone_controllers(*, directory: Path | None = None) -> list[str]:
    root = _standalone_controller_root(directory)
    return sorted(p.stem for p in (root / "controllers").iterdir()
                  if p.is_file() and not p.is_symlink()
                  and p.suffix == ".py" and p.name != "__init__.py")


def show_standalone_controller(name: str, *, directory: Path | None = None) -> dict[str, str]:
    root = _standalone_controller_root(directory)
    path = _resolve_component(root / "controllers", name, "controller")
    return {"app": "(standalone)", "name": path.stem, "path": str(path)}



def create_view(app_name: str, name: str, *,
                directory: Path | None = None) -> Path:
    """Generate a namespaced Jinja view without editing routes or templates."""

    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise ValueError(f"Invalid view name {name!r}. Use lowercase letters, numbers, underscores or hyphens (e.g. account_settings).")

    root = _scaffold_app_root(app_name, directory)
    content = ('{% extends "layout.html" %}\n\n'
               '{% block title %}' + name.replace('_', ' ').replace('-', ' ').title() + '{% endblock %}\n\n'
               '{% block content %}\n'
               f'    <h1>{name.replace("_", " ").replace("-", " ").title()}</h1>\n'
               '{% endblock %}\n')
    return _create_scaffold_file(root / "views", f"{name}.html", content)



def create_model(app_name: str, name: str, *, directory: Path | None = None, backend: str = "aurorasql") -> Path:
    """Scaffold an app-local model, without importing it or changing a database.

    The developer explicitly imports/registers the model when needed. Model
    metadata remains shared by Aurora's existing Model API.
    """

    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or keyword.iskeyword(name):
        raise ValueError(f"Invalid model name {name!r}. Use CamelCase (e.g. BlogPost).")

    if backend != "aurorasql":
        raise ValueError("Unsupported model backend")

    root = _scaffold_app_root(app_name, directory)
    folder = root / "models"

    if folder.is_symlink():
        raise ValueError("Refusing to write into a symlinked models directory.")

    created_folder = False

    if not folder.exists():
        folder.mkdir()
        created_folder = True
    elif not folder.is_dir():
        raise ValueError("models path must be a directory.")

    module = name
    source = (f'"""{name} model for the {app_name} application."""\n'
              'from aurora.database import Model, column\n\n\n'
              f'class {name}(Model):\n'
              '    id = column(int, primary_key=True)\n'
              '    # Add your model columns here.\n')

    try:
        _reject_component_collision(folder, name, "model")
        destination = _create_scaffold_file(folder, f"{module}.py", source)
    except BaseException:
        if created_folder:
            folder.rmdir()

        raise

    return destination


def list_components(resource: str, app_name: str, *,
                    directory: Path | None = None) -> list[str]:
    """List a modular app's controllers or views without importing developer code."""

    if resource not in ("controller", "view", "model"):
        raise ValueError("Unsupported component type.")

    root = _scaffold_app_root(app_name, directory)
    folder = root / {"controller": "controllers", "view": "views", "model": "models"}[resource]

    if folder.is_symlink():
        raise ValueError("Refusing to inspect a symlinked component directory.")

    if not folder.is_dir():
        return []

    suffix = ".html" if resource == "view" else ".py"
    return sorted(
        p.stem if resource != "view" else p.name
        for p in folder.iterdir()
        if p.is_file() and not p.is_symlink()
        and p.suffix == suffix and p.name != "__init__.py"
    )


def show_component(resource: str, app_name: str, name: str, *,
                   directory: Path | None = None) -> dict[str, str]:
    """Report a component's path and name; never execute or display its code."""

    if resource not in ("controller", "view", "model"):
        raise ValueError("Unsupported component type.")

    root = _scaffold_app_root(app_name, directory)
    folder = root / {"controller": "controllers", "view": "views", "model": "models"}[resource]

    if resource != "view":
        target = _resolve_component(folder, name, resource)
    else:
        entries = list_components(resource, app_name, directory=directory)
        filename = name if name.endswith(".html") else name + ".html"

        if filename not in entries:
            raise ValueError(f"View not found: {name}")

        target = folder / filename

        if target.is_symlink() or not target.is_file():
            raise ValueError("Component is not a regular file.")

    return {"app": app_name, "name": target.stem, "path": str(target)}



def _shared_component_root(resource: str, directory: Path | None = None) -> Path:
    """Locate a shared component folder in an existing MVC/modular project."""
    root = (directory or Path.cwd()).resolve()

    if (
        resource not in ("model", "view")
        or not (root / "app.py").is_file()
        or not (root / "views").is_dir()
    ):
        raise ValueError(f"Cannot use shared {resource}s here. Run this command inside an MVC project created with: aurora new myapp --mvc (or --modular)")

    if resource == "model" and not ((root / "apps").is_dir() or (root / "controllers").is_dir()):
        raise ValueError("Shared models require an MVC project created with: aurora new myapp --mvc (or --modular).")

    folder = root / ("models" if resource == "model" else "views")

    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Refusing an invalid or symlinked component directory.")

    return folder


def create_shared_component(resource: str, name: str, *, directory: Path | None = None, backend: str = "aurorasql") -> Path:
    """Scaffold a shared model or view, without registration or DB mutations."""

    if backend != "aurorasql":
        raise ValueError("Unsupported model backend")

    folder = _shared_component_root(resource, directory)

    if resource == "model":
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
            raise ValueError(f"Invalid model name {name!r}. Use CamelCase (e.g. BlogPost).")

        module = name
        filename = module + ".py"
        content = (f'"""Shared {name} model."""\nfrom aurora.database import Model, column\n\n\n'
                   f'class {name}(Model):\n    id = column(int, primary_key=True)\n'
                   '    # Add your model columns here.\n')
    elif resource == "view":
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ValueError(f"Invalid view name {name!r}. Use lowercase letters, numbers, underscores or hyphens (e.g. account_settings).")

        filename = name + ".html"
        content = ('{% extends "layout.html" %}\n\n'
                   '{% block title %}' + name.replace('_', ' ').replace('-', ' ').title() + '{% endblock %}\n\n'
                   '{% block content %}\n'
                   f'    <h1>{name.replace("_", " ").replace("-", " ").title()}</h1>\n'
                   '{% endblock %}\n')
    else:
        raise ValueError("Unsupported shared component type.")

    made = False

    if not folder.exists():
        folder.mkdir()
        made = True

    try:
        if resource == "model":
            _reject_component_collision(folder, name, "model")

        return _create_scaffold_file(folder, filename, content)
    except BaseException:
        if made:
            folder.rmdir()

        raise


def list_shared_components(resource: str, *, directory: Path | None = None) -> list[str]:
    folder = _shared_component_root(resource, directory)

    if not folder.exists():
        return []

    suffix = ".py" if resource == "model" else ".html"
    return sorted((p.stem if resource == "model" else p.name)
                  for p in folder.iterdir() if p.is_file() and not p.is_symlink()
                  and p.suffix == suffix and p.name != "__init__.py")


def show_shared_component(resource: str, name: str, *, directory: Path | None = None) -> dict[str, str]:
    folder = _shared_component_root(resource, directory)

    if resource == "model":
        path = _resolve_component(folder, name, resource)
    else:
        entries = list_shared_components(resource, directory=directory)
        filename = name if name.endswith(".html") else name + ".html"

        if filename not in entries:
            raise ValueError(f"Shared {resource} not found: {name}")

        path = folder / filename

        if path.is_symlink() or not path.is_file():
            raise ValueError("Component is not a regular file.")

    return {"app": "(shared)", "name": path.stem, "path": str(path)}
