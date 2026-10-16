# Routing and controllers

Aurora registers routes explicitly. A handler may be a function or a controller class:

```python
from aurora import Aurora, Controller

app = Aurora()


def home(request):
    return "Home"


class Article(Controller):
    def get(self, slug):
        return f"Article: {slug}"


app.add_route("/", home, name="home")
app.add_route("/articles/{slug}", Article, name="articles.show")
```

## Paths and methods

`{slug}` captures exactly one non-empty URL path segment as a string. Literal portions of paths match literally. Invalid or duplicate parameter placeholders are rejected. HTTP methods default to GET; pass `methods` explicitly when registering other verbs:

```python
app.add_route("/articles", create_article, methods=["POST"], name="articles.create")
```

`create_article` above is a placeholder for an application-defined handler, not an Aurora built-in. Routes are checked in registration order; register more specific paths before overlapping generic ones. Controller classes dispatch to HTTP-named methods such as `get`, `post`, `put`, `patch`, and `delete`. For a specific controller action, use `MyController.action("action_name")`. Both synchronous and asynchronous handlers are supported.

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
