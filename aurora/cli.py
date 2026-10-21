"""Minimal, explicit Aurora developer CLI."""
from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
import ast
import os
import keyword
from pathlib import Path
import re
import sys
import tempfile

_PROJECT = '''"""{name} Aurora application."""
from aurora import Aurora

app = Aurora()


def home(request):
    return "Hello from {name}!"


app.add_route("/", home)
'''

_MVC_APP = '''"""{name} MVC application."""
from pathlib import Path

from aurora import Aurora
from controllers.home import Home

app = Aurora(views_directory=Path(__file__).resolve().parent / "views",
             statics_directory=Path(__file__).resolve().parent / "statics")
app.route("/", Home)
'''

_MVC_HOME = '''"""Home page controller."""
from aurora import Controller, View


class Home(Controller):
    def get(self):
        return View("home", title="Welcome to {name}")
'''

_MVC_LAYOUT = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{% block title %}}Aurora MVC{{% endblock %}}</title>
    <link rel="stylesheet" href="/statics/css/site.css">
</head>
<body>
    <main>
        {{% block content %}}{{% endblock %}}
    </main>
</body>
</html>
'''

_MVC_VIEW = '''{{% extends "layout.html" %}}
{{% from "components/actions.html" import action_link %}}

{{% block title %}}{{{{ title }}}}{{% endblock %}}

{{% block content %}}
    <h1>{{{{ title }}}}</h1>
    <p>Your Aurora MVC project is ready.</p>
    {{{{ action_link("Aurora documentation", "https://auroramvc.com") }}}}
{{% endblock %}}
'''


_MODULAR_APP = '''"""{name} modular MVC application."""
from pathlib import Path

from aurora import Aurora
from apps.home.controllers.home import Home
from apps.blog.controllers.blog import Blog
# aurora:imports

ROOT = Path(__file__).resolve().parent
# Shared templates live in views/; app templates are namespaced logically.
app = Aurora(
    views_directory=ROOT / "views",
    view_namespaces={{
        "home": ROOT / "apps" / "home" / "views",
        "blog": ROOT / "apps" / "blog" / "views",
        # aurora:views
    }},
    statics_directory=ROOT / "statics",
)
app.route("/", Home)
app.route("/blog", Blog)
# aurora:routes
'''

_MODULAR_HOME = '''"""Home page controller."""
from aurora import Controller, View


class Home(Controller):
    def get(self):
        return View("home/index", title="Welcome to {name}")
'''

_MODULAR_BLOG = '''"""Blog page controller."""
from aurora import Controller, View


class Blog(Controller):
    def get(self):
        return View("blog/index", title="Blog")
'''

_MODULAR_LAYOUT = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{{% block title %}}{{{{ title }}}}{{% endblock %}}</title>
    <link rel="stylesheet" href="/statics/css/site.css">
</head>
<body>
    {{% include "navigation.html" %}}
    <main>
        {{% block content %}}{{% endblock %}}
    </main>
</body>
</html>
'''

_MODULAR_HOME_VIEW = '''{{% extends "layout.html" %}}

{{% block content %}}
    <h1>{{{{ title }}}}</h1>
    <p>Your modular Aurora project is ready.</p>
{{% endblock %}}
'''

_MODULAR_BLOG_VIEW = '''{{% extends "layout.html" %}}
{{% from "components.html" import action_link %}}

{{% block content %}}
    <h1>{{{{ title }}}}</h1>
    <p>Your blog app is ready.</p>
    {{{{ action_link("Back to home", "/") }}}}
{{% endblock %}}
'''


# Includes and macros are native Jinja2 features: no extra Aurora runtime API.
_MVC_COMPONENTS = '''{% macro action_link(label, href) -%}
    <a class="action-link" href="{{ href }}">{{ label }}</a>
{%- endmacro %}
'''

_MODULAR_NAVIGATION = '''<nav aria-label="Main navigation">
    <a href="/">Home</a>
    <a href="/blog">Blog</a>
</nav>
'''

_MODULAR_COMPONENTS = '''{% macro action_link(label, href) -%}
    <a class="action-link" href="{{ href }}">{{ label }}</a>
{%- endmacro %}
'''


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
                "apps/blog/views", "views", "statics",
            ):
                folder = root / relative
                folder.mkdir()
                if relative in {"apps", "apps/home", "apps/home/controllers", "apps/blog", "apps/blog/controllers"}:
                    (folder / "__init__.py").write_text("", encoding="utf-8")
            (root / "apps/home/controllers/home.py").write_text(
                _MODULAR_HOME.format(name=name), encoding="utf-8"
            )
            (root / "apps/blog/controllers/blog.py").write_text(
                _MODULAR_BLOG, encoding="utf-8"
            )
            (root / "views/layout.html").write_text(_MODULAR_LAYOUT.format(), encoding="utf-8")
            (root / "views/navigation.html").write_text(_MODULAR_NAVIGATION, encoding="utf-8")
            (root / "views/components.html").write_text(_MODULAR_COMPONENTS, encoding="utf-8")
            (root / "apps/home/views/index.html").write_text(_MODULAR_HOME_VIEW.format(), encoding="utf-8")
            (root / "apps/blog/views/index.html").write_text(_MODULAR_BLOG_VIEW.format(), encoding="utf-8")
        elif mvc:
            (root / "controllers").mkdir()
            (root / "controllers" / "__init__.py").write_text("", encoding="utf-8")
            (root / "controllers" / "home.py").write_text(
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
                "/* Public Aurora starter styles. Edit or replace freely. */\n"
                "body { font-family: system-ui, sans-serif; margin: 2rem auto; "
                "max-width: 56rem; padding: 0 1rem; line-height: 1.6; }\n"
                "nav a { margin-right: 1rem; }\n",
                encoding="utf-8",
            )
        # Explicit, inert database starter files. Never open or create a database
        # during scaffolding; developers register models before planning migrations.
        (root / "aurora_databases.json").write_text(
            json.dumps({"databases": {"default": {"url": "sqlite:///data.sqlite3"}}}, indent=2) + "\n",
            encoding="utf-8",
        )
        (root / "aurora_models.json").write_text(
            json.dumps({"databases": {}}, indent=2) + "\n", encoding="utf-8",
        )
        (root / "migrations").mkdir()
        (root / "backups").mkdir()
        (root / "README.md").write_text(
            f"# {name}\n\nRun from the parent directory: "
            f"`aurora run {name}.app:app`\n\n"
            "## Database setup\n\n"
            "- `aurora_databases.json` configures the `default` SQLite alias.\n"
            "- `aurora_models.json` intentionally starts empty. Register trusted "
            "`MODULE:CLASS` model references under `databases.default` before using "
            "model-aware migration commands.\n"
            "- No SQLite file or tables are created by scaffolding. Initialize "
            "your database explicitly before inspection or migrations.\n"
            "- `aurora db list` shows configured aliases; "
            "`aurora db sync --dry-run` previews changes after setup.\n"
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
    route = f"/{name}"
    if f'app.route("{route}"' in source:
        raise ValueError(f"Route already registered: {route}")
    class_name = "".join(part.capitalize() for part in name.split("_"))
    updated = source.replace("# aurora:imports", f"from apps.{name}.controllers.{name} import {class_name}\n# aurora:imports")
    updated = updated.replace("        # aurora:views", f'        "{name}": ROOT / "apps" / "{name}" / "views",\n        # aurora:views')
    updated = updated.replace("# aurora:routes", f'app.route("/{name}", {class_name})\n# aurora:routes')
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
        (controllers / f"{name}.py").write_text(
            f'"""{class_name} controller."""\nfrom aurora import Controller, View\n\n\n'
            f'class {class_name}(Controller):\n'
            '    def get(self):\n'
            f'        return View("{name}/index", title="{name.replace("_", " ").title()}")\n',
            encoding="utf-8",
        )
        view_dir = staging / "views"
        view_dir.mkdir()
        (view_dir / "index.html").write_text(
            '{% extends "layout.html" %}\n'
            '{% from "components.html" import action_link %}\n\n'
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
        raise ValueError("Controller name must be CamelCase (e.g. AccountSettings).")
    root = _scaffold_app_root(app_name, directory)
    project = root.parent.parent
    app_file = project / "app.py"
    source = app_file.read_text(encoding="utf-8")
    anchors = ("# aurora:imports", "# aurora:routes")
    if any(source.count(anchor) != 1 for anchor in anchors):
        raise ValueError("app.py must have unique Aurora import and route anchors.")
    module = name.lower()
    destination = root / "controllers" / f"{module}.py"
    if destination.exists():
        raise FileExistsError(f"Controller already exists: {destination}")
    route = f"/{app_name}/{module.replace('_', '-')}"
    # Keep the existing app's base route untouched and avoid duplicate registrations.
    if f'app.route("{route}"' in source or f"app.route('{route}'" in source:
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
        raise ValueError("Standalone controllers require an existing non-modular MVC project.")
    return root


def create_standalone_controller(name: str, *, directory: Path | None = None) -> Path:
    """Create and register a controller in a standalone MVC scaffold."""
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or keyword.iskeyword(name):
        raise ValueError("Controller name must be CamelCase (e.g. AccountSettings).")
    root = _standalone_controller_root(directory)
    app_file = root / "app.py"
    source = app_file.read_text(encoding="utf-8")
    # The original standalone scaffold has no anchors. Require a recognizable
    # Aurora application before appending the registration.
    if not re.search(r"(?m)^app\s*=\s*Aurora\(", source):
        raise ValueError("Cannot safely register controller: app.py has no standard Aurora app assignment.")
    module = name.lower()
    destination = root / "controllers" / f"{module}.py"
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Controller already exists: {destination}")
    route = f"/{module}"
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
    if name not in list_standalone_controllers(directory=directory):
        raise ValueError(f"Controller not found: {name}")
    root = _standalone_controller_root(directory)
    path = root / "controllers" / (name + ".py")
    return {"app": "(standalone)", "name": name, "path": str(path)}


def create_view(app_name: str, name: str, *,
                directory: Path | None = None) -> Path:
    """Generate a namespaced Jinja view without editing routes or templates."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
        raise ValueError("View name must use lowercase letters, numbers, '_' or '-'.")
    root = _scaffold_app_root(app_name, directory)
    content = ('{% extends "layout.html" %}\n\n'
               '{% block content %}\n'
               f'    <h1>{name.replace("_", " ").replace("-", " ").title()}</h1>\n'
               '{% endblock %}\n')
    return _create_scaffold_file(root / "views", f"{name}.html", content)



def create_model(app_name: str, name: str, *, directory: Path | None = None) -> Path:
    """Scaffold an app-local model, without importing it or changing a database.

    The developer explicitly imports/registers the model when needed. Model
    metadata remains shared by Aurora's existing Model API.
    """
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name) or keyword.iskeyword(name):
        raise ValueError("Model name must be CamelCase (e.g. BlogPost).")
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
    module = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    source = (f'"""{name} model for the {app_name} application."""\n'
              'from aurora.database import Model, column\n\n\n'
              f'class {name}(Model):\n'
              '    id = column(int, primary_key=True)\n'
              '    # Add your model columns here.\n')
    try:
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
    entries = list_components(resource, app_name, directory=directory)
    valid = name in entries or (resource == "view" and name + ".html" in entries)
    if not valid:
        raise ValueError(f"{resource.title()} not found: {name}")
    root = _scaffold_app_root(app_name, directory)
    filename = name if resource == "view" and name.endswith(".html") else (
        name + (".html" if resource == "view" else ".py"))
    target = (
        root
        / {"controller": "controllers", "view": "views", "model": "models"}[resource]
        / filename
    )
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
        raise ValueError("Shared components require an existing MVC project.")
    if resource == "model" and not ((root / "apps").is_dir() or (root / "controllers").is_dir()):
        raise ValueError("Shared models require an MVC project.")
    folder = root / ("models" if resource == "model" else "views")
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Refusing an invalid or symlinked component directory.")
    return folder


def create_shared_component(resource: str, name: str, *, directory: Path | None = None) -> Path:
    """Scaffold a shared model or view, without registration or DB mutations."""
    folder = _shared_component_root(resource, directory)
    if resource == "model":
        if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", name):
            raise ValueError("Model name must be CamelCase.")
        module = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        filename = module + ".py"
        content = (f'"""Shared {name} model."""\nfrom aurora.database import Model, column\n\n\n'
                   f'class {name}(Model):\n    id = column(int, primary_key=True)\n'
                   '    # Add your model columns here.\n')
    elif resource == "view":
        if not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ValueError("View name must use lowercase letters, digits, _ or -.")
        filename = name + ".html"
        content = ('{% extends "layout.html" %}\n\n{% block content %}\n'
                   f'    <h1>{name.replace("_", " ").replace("-", " ").title()}</h1>\n'
                   '{% endblock %}\n')
    else:
        raise ValueError("Unsupported shared component type.")
    made = False
    if not folder.exists():
        folder.mkdir()
        made = True
    try:
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
    entries = list_shared_components(resource, directory=directory)
    if name not in entries and not (resource == "view" and name + ".html" in entries):
        raise ValueError(f"Shared {resource} not found: {name}")
    folder = _shared_component_root(resource, directory)
    filename = (
        name if resource == "view" and name.endswith(".html")
        else name + (".py" if resource == "model" else ".html")
    )
    path = folder / filename
    if path.is_symlink() or not path.is_file():
        raise ValueError("Component is not a regular file.")
    return {"app": "(shared)", "name": path.stem, "path": str(path)}


def main(argv: list[str] | None = None) -> int:
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
    # Read-only named database commands; no model imports or schema writes.
    db_group = sub.add_parser("db", help="Inspect databases and safely migrate SQLite schemas")
    db_actions = db_group.add_subparsers(dest="action", required=True)
    for verb in ("list", "show", "check", "inspect", "plan", "history", "status", "validate"):
        command = db_actions.add_parser(verb, help=("Read applied SQLite migrations (or --history-file for legacy store)" if verb == "history" else "Read applied migrations and optionally compare selected models" if verb == "status" else {"list": "List configured database aliases", "show": "Show configured database details", "check": "Check existing SQLite schema readability", "inspect": "Inspect existing SQLite schema", "plan": "Preview model-to-schema differences", "validate": "Review migration readiness"}.get(verb, f"Read-only database {verb}")))
        command.add_argument("--config", default="aurora_databases.json", help="JSON database config path")
        if verb == "show":
            command.add_argument("alias", nargs="?", default="default")
        if verb in ("check", "inspect", "plan", "history", "status", "validate"):
            command.add_argument("--database", default="default", help="Configured database alias")
        if verb in ("plan", "validate", "status"):
            command.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS",
                                 help="Override project manifest with explicit Aurora models")
            command.add_argument("--models-config", default="aurora_models.json", help="Explicit project model manifest JSON path")
        if verb == "plan":
            command.add_argument("--explain", action="store_true", help="Explain classifications and reasons (read-only)")
            command.add_argument("--format", choices=("text", "json"), default="text", help="Output format (default: text)")
            command.add_argument("--check", action="store_true", help="CI check: return exit code 2 if schema drift is detected")
        if verb == "history":
            command.add_argument("--history-file",
                                 help="Optional legacy dedicated history SQLite file; otherwise reads applied migrations from target database")
    sync = db_actions.add_parser('sync', aliases=['s'], help='Preview or safely apply SQLite CREATE TABLE / nullable ADD COLUMN migrations')
    sync.add_argument('--config', default='aurora_databases.json', help='Database configuration JSON path')
    sync.add_argument('--database', default='default', help='Configured database alias (default: default)')
    sync.add_argument('--model', action='append', metavar='MODULE:CLASS', help='Explicit trusted Aurora model; repeat for multiple models')
    sync.add_argument('--models-config', default='aurora_models.json', help='Explicit project model manifest JSON path')
    sync.add_argument('--explain', action='store_true', help='Explain planned changes during --dry-run only')
    sync.add_argument('--format', choices=('text', 'json'), default='text', help='Dry-run output format (default: text)')
    sync.add_argument('--dry-run', action='store_true', help='Preview changes only; no files or schema writes')
    sync.add_argument('--check', action='store_true', help='CI check during --dry-run: exit 2 for schema drift')
    sync.add_argument('--approve', help='Reviewed SHA-256 digest; APPLY still required')
    sync.add_argument('--name', help='New migration review filename stem (required to apply)')
    sync.add_argument('--output-dir', default='migrations', help='Existing review directory (default: migrations)')
    sync.add_argument('--backup', help='New backup filename in an existing directory (required to apply)')
    migration_group = db_actions.add_parser("migration", help="Create/show reviews or apply approved SQLite migrations")
    migration_actions = migration_group.add_subparsers(dest="migration_action", required=True)
    create = migration_actions.add_parser("create", help="Save an advisory JSON review (no SQL)")
    create.add_argument("--config", default="aurora_databases.json")
    create.add_argument("--database", default="default")
    create.add_argument("--model", action="append", default=[], metavar="MODULE:CLASS")
    create.add_argument("--models-config", default="aurora_models.json", help="Explicit project model manifest JSON path")
    create.add_argument("--name", required=True, help="New review filename stem")
    create.add_argument("--output-dir", default="migrations", help="Existing output directory")
    show_review_cmd = migration_actions.add_parser("show", help="Verify and display a review JSON file")
    show_review_cmd.add_argument("path")
    apply_cmd = migration_actions.add_parser("apply", help="Apply approved SQLite additive review (WRITES DATABASE)")
    apply_cmd.add_argument("path", help="Existing reviewed JSON file")
    apply_cmd.add_argument("--config", default="aurora_databases.json")
    apply_cmd.add_argument("--approve", required=True, help="Exact plan SHA-256 shown by migration show")
    apply_cmd.add_argument("--backup", required=True, help="New SQLite backup path (must not exist)")
    # Destructive operations remain deliberately unavailable.
    run = sub.add_parser("run", help="Run an ASGI application with Uvicorn")
    run.add_argument("application", nargs="?", default="app:app", help="ASGI import path (default: app:app)")
    run.add_argument("--host", default="127.0.0.1")
    run.add_argument("--port", type=int, default=8000)
    run.add_argument("--no-reload", action="store_true")
    args = parser.parse_args(argv)
    if args.command == 'db' and args.action in ('sync', 's'):
        from aurora.database.cli_sync import sync_database
        def confirm(text):
            print(f"Proposed changes:\n{text}\nPlan SHA-256: {args.approve}")
            try:
                return input('Apply this migration? Type APPLY: ').strip() == 'APPLY'
            except EOFError:
                return False
        try:
            if args.explain and not args.dry_run:
                raise ValueError('--explain requires --dry-run')
            if args.check and not args.dry_run:
                raise ValueError('--check requires --dry-run')
            if args.format == 'json' and not args.dry_run:
                raise ValueError('--format json requires --dry-run')
            from aurora.database.cli_model_manifest import select_model_refs
            model_refs = select_model_refs(explicit=args.model, database=args.database, manifest=args.models_config)
            if args.format == 'json':
                from aurora.database.cli_reports import report_for_refs
                import json
                report = report_for_refs(config_path=args.config, alias=args.database, model_refs=model_refs)
                print(json.dumps(report, indent=2, sort_keys=True))
                return 2 if args.check and report['schema_status'] == 'DRIFT' else 0
            if args.check:
                from aurora.database.cli_reports import report_for_refs
                report = report_for_refs(config_path=args.config, alias=args.database, model_refs=model_refs)
                print(f"Schema status: {report['schema_status']} ({len(report['changes'])} difference(s))")
                return 2 if report['schema_status'] == 'DRIFT' else 0
            status, digest, tables = sync_database(
                config_path=args.config, alias=args.database, model_refs=model_refs,
                dry_run=args.dry_run, approve=args.approve, name=args.name,
                output_dir=args.output_dir, backup_path=args.backup,
                confirm=None if args.dry_run else confirm)
            print(f"Database: {args.database}\nStatus: {status}\nPlan SHA-256: {digest}")
            for table in tables:
                print(f"  ADD COLUMN {table}" if "." in table else f"  CREATE TABLE {table}")
            if args.explain:
                from aurora.database.cli_inspection import load_cli_database_config, plan_database
                from aurora.database.cli_diagnostics import explain_plan
                from aurora.database.model import Model
                import importlib
                models = []
                for ref in model_refs:
                    module, _, class_name = ref.partition(':')
                    cls = getattr(importlib.import_module(module), class_name)
                    if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
                        raise ValueError(f'Not an Aurora model: {ref}')
                    models.append(cls)
                config, base_dir = load_cli_database_config(args.config)
                plan = plan_database(config, args.database, models, base_dir=base_dir)
                print(explain_plan(plan, models))
        except (ValueError, OSError, ImportError, AttributeError, __import__('sqlite3').Error) as exc:
            parser.exit(1, f"aurora: {exc}\n")
        return 0
    if args.command == "db" and args.action == "migration" and args.migration_action == "apply":
        from aurora.database.sqlite_migration_executor import apply_create_tables
        try:
            manifest, tables = apply_create_tables(args.path, config_path=args.config,
                approval=args.approve, backup_path=args.backup)
            print(f"Migration applied: {len(tables)} operation(s). Backup: {manifest.filename} ({manifest.sha256_hex})")
        except (ValueError, OSError, ImportError, AttributeError, __import__('sqlite3').Error) as exc:
            parser.exit(1, f"aurora: {exc}\n")
        return 0
    if args.command == "db" and args.action == "migration" and args.migration_action == "show":
        from aurora.database.cli_review import show_review
        try:
            data, plan = show_review(args.path)
            print(f"Database: {data['database_alias']}\nPlan SHA-256: {data['plan_sha256']}\n{plan.format_text()}\nExecution disabled. This command only displays the review; apply separately with explicit approval and backup.")
        except (ValueError, OSError, UnicodeError, __import__('json').JSONDecodeError) as exc:
            parser.exit(1, f"aurora: {exc}\n")
        return 0
    if args.command == "db":
        from aurora.database.cli_inspection import (
            load_cli_database_config, describe_database, inspect_database,
            inspect_database_details, plan_database, inspect_history_file,
        )
        try:
            config, base_dir = load_cli_database_config(args.config)
            if args.action == "list":
                for alias in config.aliases():
                    info = describe_database(config, alias)
                    print(f"{alias} ({info['dialect']})")
            elif args.action == "show":
                info = describe_database(config, args.alias)
                print(f"Alias: {info['alias']}\nDialect: {info['dialect']}\nDriver: {info['driver']}")
            elif args.action == "check":
                count = inspect_database(config, args.database, base_dir=base_dir)
                print(f"Database: {args.database}\nSQLite schema readable; {count} user table(s).")
            elif args.action == "inspect":
                schema = inspect_database_details(config, args.database, base_dir=base_dir)
                print(f"Database: {args.database}\nTables: {len(schema.tables)}")
                for table in schema.tables:
                    print(f"  {table.name}: {len(table.columns)} column(s), "
                          f"{len(table.indexes)} index(es), {len(table.foreign_keys)} foreign key(s)")
            elif args.action in ("plan", "validate") or (args.action == "migration" and args.migration_action == "create"):
                from aurora.database.cli_model_manifest import select_model_refs
                selected_refs = select_model_refs(explicit=args.model, database=args.database, manifest=args.models_config)
                import importlib
                from aurora.database.model import Model
                models = []
                for reference in selected_refs:
                    module_name, separator, class_name = reference.partition(":")
                    if not separator or not module_name or not class_name or not class_name.isidentifier():
                        raise ValueError("--model must be MODULE:CLASS")
                    cls = getattr(importlib.import_module(module_name), class_name, None)
                    if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
                        raise ValueError(f"Not an Aurora model: {reference}")
                    models.append(cls)
                plan = plan_database(config, args.database, models, base_dir=base_dir)
                if args.action == "plan":
                    if args.format == 'json':
                        from aurora.database.cli_reports import report_for_plan
                        import json
                        report = report_for_plan(args.database, selected_refs, plan, models)
                        print(json.dumps(report, indent=2, sort_keys=True))
                        return 2 if args.check and report['schema_status'] == 'DRIFT' else 0
                    print(f"Database: {args.database}\n{plan.format_text()}")
                    if args.check:
                        from aurora.database.cli_reports import report_for_plan
                        report = report_for_plan(args.database, selected_refs, plan, models)
                        return 2 if report['schema_status'] == 'DRIFT' else 0
                    if args.explain:
                        from aurora.database.cli_diagnostics import explain_plan
                        print(explain_plan(plan, models))
                elif args.action == "validate":
                    from aurora.database.cli_review import validate_plan
                    review, readiness = validate_plan(plan)
                    print(f"Database: {args.database}\n{review.format_text()}\n{readiness.format_text()}")
                else:
                    from aurora.database.cli_review import create_review
                    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", args.name):
                        raise ValueError("Invalid migration review name")
                    folder = Path(args.output_dir).expanduser()
                    target = create_review(folder / (args.name + ".json"), alias=args.database,
                                           models=selected_refs, plan=plan)
                    print(f"Created NON-EXECUTABLE review: {target}")
            elif args.action in ("history", "status") and (args.action == "status" or not args.history_file):
                from aurora.database.cli_migration_status import read_applied_migrations
                present, records = read_applied_migrations(config, args.database, base_dir=base_dir)
                print(f"Database: {args.database}\nApplied migrations: {len(records)}")
                if not present:
                    print("Migration history: not initialized")
                for digest, record_alias, applied_at, backup_name, backup_digest in records:
                    print(f"  {digest}: applied {applied_at}; backup {backup_name} (SHA-256: {backup_digest})")
                if args.action == "status" and (args.model or Path(args.models_config).expanduser().is_file()):
                    from aurora.database.cli_model_manifest import select_model_refs
                    selected_refs = select_model_refs(explicit=args.model, database=args.database, manifest=args.models_config)
                    from aurora.database.cli_schema_status import compare_selected_models
                    plan = compare_selected_models(config, args.database, selected_refs, base_dir=base_dir)
                    print(f"Selected models: {len(selected_refs)}")
                    if plan.is_empty:
                        print("Schema status: MATCH (selected models only)")
                    else:
                        print(f"Schema status: DRIFT ({len(plan.steps)} difference(s); selected models only)")
                        for step in plan.steps:
                            location = step.table + ('.' + step.column if step.column else '')
                            print(f"  [{step.risk.upper()}] {step.kind}: {location}")
                    print("History and schema status are independent; neither proves the other.")
            elif args.action == "history":
                config.resolve(args.database)  # Legacy separate history store, explicit opt-in.
                history_path = Path(args.history_file).expanduser()
                if not history_path.is_absolute():
                    history_path = base_dir / history_path
                records = inspect_history_file(history_path)
                print(f"Database: {args.database}\nHistory records: {len(records)}")
                for migration_id, state, recorded_at in records:
                    print(f"  {migration_id}: {state} ({recorded_at})")
        except (ValueError, OSError, ImportError, AttributeError, __import__("sqlite3").Error) as exc:
            parser.exit(1, f"aurora: {exc}\n")
        return 0
    if args.command == "model" and args.action in ("register", "registered"):
        from aurora.database.cli_model_registration import register_model, registered_models
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
                parser.error("Do not combine legacy positional app and --app.")
            app_name, name = args.first, args.second
        else:
            app_name, name = args.app_option, args.first
            if args.action == "list" and args.first is not None and args.app_option is None:
                app_name, name = args.first, None  # Legacy: resource list APP
        if args.action != "list" and not name:
            parser.error(f"{resource} {args.action} requires a component name.")
        if args.action == "add" and os.environ.get("AURORA_ENV", "development").lower() == "production":
            parser.exit(1, "aurora: Refusing to modify project in production environment.\n")
        try:
            if args.action == "add":
                if app_name is None:
                    destination = (create_standalone_controller(name) if resource == "controller"
                                   else create_shared_component(resource, name))
                else:
                    destination = {"controller": create_controller, "view": create_view,
                                   "model": create_model}[resource](app_name, name)
                note = {"controller": " (route registered)", "view": " (routes unchanged)",
                        "model": " (database unchanged; import model explicitly)"}[resource]
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
    uvicorn.run(args.application, host=args.host, port=args.port, reload=not args.no_reload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
