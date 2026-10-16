"""modular modular MVC application."""
from pathlib import Path

from aurora import Aurora
from apps.home.controllers.home import Home
from apps.blog.controllers.blog import Blog
# aurora:imports

ROOT = Path(__file__).resolve().parent
# Shared templates live in views/; app templates are namespaced logically.
app = Aurora(
    views_directory=ROOT / "views",
    view_namespaces={
        "home": ROOT / "apps" / "home" / "views",
        "blog": ROOT / "apps" / "blog" / "views",
        # aurora:views
    },
    statics_directory=ROOT / "statics",
)
app.route("/", Home)
app.route("/blog", Blog)
# aurora:routes
