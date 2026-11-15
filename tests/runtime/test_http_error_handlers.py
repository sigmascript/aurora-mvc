"""F02: HTTP error handlers are opt-in, bounded and safe at ASGI boundary."""

import asyncio

import pytest

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.response import JSONResponse, Response
from aurora.view import View


def exchange(app, path="/missing", method="GET"):
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        events.append(event)

    asyncio.run(app({"type": "http", "path": path, "method": method,
                     "query_string": b"", "headers": []}, receive, send))
    start = next(e for e in events if e["type"] == "http.response.start")
    body = b"".join(e.get("body", b"") for e in events if e["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), body


def test_unregistered_errors_preserve_previous_default_behavior():
    app = Application()
    app.add_route("/get", lambda request: "ok")
    assert exchange(app)[:1] == (404,)
    assert exchange(app, "/get", "POST")[0] == 405
    assert exchange(app, "/get", "POST")[1][b"allow"] == b"GET, HEAD"


def test_text_and_json_handlers_preserve_status_and_allow():
    app = Application()
    app.add_route("/get", lambda request: "ok")

    @app.error(404)
    def missing(request, error):
        assert error.status_code == 404
        return Response("custom missing", headers={"X-Error": "yes"})

    @app.error(405)
    async def method_error(request, error):
        result = JSONResponse({"message": error.detail})
        result.headers["allow"] = "DELETE"  # Never override the router's Allow.
        return result

    assert exchange(app)[0] == 404
    assert exchange(app)[2] == b"custom missing"
    assert exchange(app)[1][b"x-error"] == b"yes"
    status, headers, body = exchange(app, "/get", "POST")
    assert status == 405 and headers[b"allow"] == b"GET, HEAD"
    assert headers[b"content-type"].startswith(b"application/json")
    assert b"Method Not Allowed" in body
    assert exchange(app, "/get", "HEAD") == (200, {
        b"content-type": b"text/plain; charset=utf-8", b"content-length": b"2"}, b"")


def test_explicit_http_exception_and_unexpected_500():
    app = Application()

    @app.error(403)
    def forbidden(request, error):
        return "Forbidden by policy"

    @app.error(500)
    def failure(request, error):
        assert error.detail == "Internal Server Error"
        return "custom server error"

    def forbidden_route(request):
        raise HTTPException(403, "private internal explanation")

    def crash(request):
        raise RuntimeError("private token: secret")

    app.add_route("/forbidden", forbidden_route)
    app.add_route("/crash", crash)
    assert exchange(app, "/forbidden") == (
        403, {b"content-type": b"text/plain; charset=utf-8", b"content-length": b"19"},
        b"Forbidden by policy",
    )
    status, _, body = exchange(app, "/crash")
    assert (status, body) == (500, b"custom server error")


def test_view_errors_use_namespaced_renderer_and_context(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    (views / "error.html").write_text("<h2>{{ reason }} {{ constants.SITE_NAME }}</h2>")
    app = Application(views_directory=views)
    app.constants = {"SITE_NAME": "Aurora"}
    app.renderer.environment.globals["constants"] = app.constants

    @app.error(404)
    def missing(request, error):
        return View("error", reason="Missing")

    status, headers, body = exchange(app)
    assert status == 404
    assert headers[b"content-type"] == b"text/html; charset=utf-8"
    assert b"Missing Aurora" in body


def test_bad_custom_handler_fails_closed_without_recursive_reentry():
    app = Application()
    called = []

    @app.error(404)
    def bad(request, error):
        called.append("404")
        raise RuntimeError("sensitive error")

    @app.error(500)
    def other(request, error):
        called.append("500")
        return "should not run"

    assert exchange(app) == (500, {
        b"content-type": b"text/plain; charset=utf-8", b"content-length": b"21"
    }, b"Internal Server Error")
    assert called == ["404"]


def test_missing_custom_view_falls_back_safely(tmp_path):
    app = Application(views_directory=tmp_path)

    @app.error(404)
    def missing(request, error):
        return View("never-present")

    status, _, body = exchange(app)
    assert (status, body) == (500, b"Internal Server Error")
    assert str(tmp_path).encode() not in body


def test_handler_validation_and_response_status_invariant():
    app = Application()
    for value in (False, "404", 200, 600):
        with pytest.raises(ValueError):
            app.add_error_handler(value, lambda request, error: "bad")
    with pytest.raises(TypeError):
        app.add_error_handler(404, None)

    @app.error(404)
    def misleading(request, error):
        return Response("custom", status_code=201)

    assert exchange(app)[0] == 404
