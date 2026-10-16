"""mvc MVC application."""
from pathlib import Path

from aurora import Aurora
from controllers.home import Home

app = Aurora(views_directory=Path(__file__).resolve().parent / "views",
             statics_directory=Path(__file__).resolve().parent / "statics")
app.route("/", Home)
