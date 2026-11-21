# Routing and controllers

Aurora registers routes explicitly. Use `app.add_route()` for a function handler and `app.route()` for a Controller subclass:

```python
from aurora import Aurora, Controller

app = Aurora()


def home(request):
    return "Home"


class Article(Controller):
    def get(self, slug):
        return f"Article: {slug}"


app.add_route("/", home, name="home")
app.route("/articles/{slug}", Article, name="articles.show")
```

## Paths and methods

`{slug}` captures exactly one non-empty URL path segment as a string. Literal portions of paths match literally. Invalid or duplicate parameter placeholders are rejected. HTTP methods default to GET; pass `methods` explicitly when registering other verbs:

```python
app.add_route("/articles", create_article, methods=["POST"], name="articles.create")
```

`create_article` above is a placeholder for an application-defined handler, not an Aurora built-in. Routes are checked in registration order; register more specific paths before overlapping generic ones. Controller classes dispatch to HTTP-named methods such as `get`, `post`, `put`, `patch`, and `delete`. For a specific controller action, use `MyController.action("action_name")`. Both synchronous and asynchronous handlers are supported.

## How HTTP methods are registered

There are two separate routing conventions. A standalone handler uses `app.add_route()` and defaults to GET; its optional **plural** `methods=[...]` lists every allowed HTTP verb:

```python
from aurora import Aurora

app = Aurora()


def status(request):
    return "Ready"


def notes_handler(request):
    if request.method == "POST":
        return {"created": True}, 201
    return {"notes": []}


app.add_route("/status/", status)  # GET (and supported HEAD behavior)
app.add_route("/notes/", notes_handler, methods=["GET", "POST"])
```

A controller class instead uses `app.route()`, which **discovers its implemented HTTP-named methods**. You do not repeat `methods` in `app.route()`:

```python
from aurora import Controller


class Notes(Controller):
    def get(self):
        return {"notes": []}

    def post(self):
        data = self.request.json()  # Synchronous controller
        return {"received": data}, 201


app.route("/notes/", Notes)
```

The CLI `aurora controller add Notes --methods "get,post"` generates method stubs; the class methods, not CLI arguments on the route, determine what `app.route()` registers. A POST to a GET-only route is rejected with **405 Method Not Allowed** and an `Allow` header. Route names, explicit handlers and named controller actions still use their existing APIs.

For full MVC/Modular model imports, CRUD operations, form/JSON parsing and safe transaction examples, see [AuroraSQL and migrations](database.md#use-models-from-application-code).

## Named routes and URL generation

Generate paths by route name instead of duplicating URL strings:

```python
# Outside a request handler:
app.url_for("articles.show", slug="welcome")  # /articles/welcome

# Inside a request handler:
from aurora import url_for

article_url = url_for("articles.show", slug="welcome")
```

`url_for()` requires an active request; use `app.url_for()` outside request handling. Route parameters must match the registered placeholders exactly. Values are URL-encoded as individual path segments; empty segments, `.` or `..`, and slash or backslash characters are rejected.

For locale-prefixed routes, see [Multilingual applications](multilingual.md). This guide describes the current alpha API; interfaces may evolve before a stable release.
