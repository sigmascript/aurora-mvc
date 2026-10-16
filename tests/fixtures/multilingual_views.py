"""Request-scoped localized template navigation, without sessions.

Run: python -m uvicorn examples.multilingual_views:app --reload
"""
from pathlib import Path
from aurora.core.application import Application
from aurora.view import View

app = Application(
    views_directory=Path(__file__).parent / "multilingual_views_templates",
    locales=["en", "ku", "ar"], default_locale="en",
    locale_cookie_name="preferred_language",
)


def home(request):
    return View("home")


def article(request, id):
    return View("article", article_id=id)


app.add_route("/", home, name="home")
app.add_route("/articles/{id}", article, name="article")
