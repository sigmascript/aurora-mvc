"""Minimal session-free multilingual navigation example.

Run: python -m uvicorn examples.multilingual_navigation:app --reload
"""
from aurora.core.application import Application
from aurora.core.response import RedirectResponse, Response
from aurora.core.http_exception import HTTPException

app = Application(
    locales=["en", "ku", "ar"],
    default_locale="en",
    locale_cookie_name="preferred_language",
)


@app.add_middleware
async def locale_header(request, call_next):
    response = await call_next(request)
    if request.locale is not None:
        response.headers["Content-Language"] = request.locale
    return response


def home(request):
    return Response(f"Home language: {request.locale}")


def article(request, id):
    # In AIO CMS, a controller/service would resolve translated content here.
    return Response(f"Article {id}; language: {request.locale}")


def switch_article(request, language, id):
    # Demonstration only: production switches should use POST + CSRF.
    # This explicit route demonstrates switching a named resource, not an
    # arbitrary client-provided redirect target. The route id is a shared key.
    if language not in app.locales:
        raise HTTPException(404, "Unsupported language")
    target = app.localized_url_for("article", language=language, id=id)
    response = RedirectResponse(target, status_code=303)
    return app.remember_locale(response, language, secure=False)  # local HTTP only


def switch_home(request, language):
    if language not in app.locales:
        raise HTTPException(404, "Unsupported language")
    target = app.localized_url_for("home", language=language)
    response = RedirectResponse(target, status_code=303)
    return app.remember_locale(response, language, secure=False)  # local HTTP only


app.add_route("/", home, name="home")
app.add_route("/articles/{id}", article, name="article")
app.add_route("/switch/home/{language}", switch_home, name="switch.home")
app.add_route("/switch/article/{language}/{id}", switch_article, name="switch.article")
