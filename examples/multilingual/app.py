"""Locale-prefixed routes with a language preference cookie (optional)."""
from aurora import Aurora
from aurora.core.response import RedirectResponse, Response

app = Aurora(locales=["en", "ku", "ar"], default_locale="en",
             locale_cookie_name="preferred_language")


def home(request):
    return Response(f"Welcome! Current language: {request.locale}")


def article(request, id):
    return Response(f"Article {id}; language: {request.locale}")


app.add_route("/", home, name="home")
app.add_route("/articles/{id}/", article, name="article")


@app.add_middleware
async def redirect_root(request, call_next):
    """Give visitors a predictable entry point without changing locale routing."""
    if request.path == "/" and request.method in {"GET", "HEAD"}:
        return RedirectResponse("/en/", status_code=302)
    return await call_next(request)
