"""Approved F02/F03 HTTP route and method semantics, exercised via ASGI."""

import asyncio

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.response import Response


def exchange(app, path, method="GET"):
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(event):
        events.append(event)

    asyncio.run(
        app(
            {"type": "http", "method": method, "path": path, "headers": [], "query_string": b""},
            receive,
            send,
        )
    )
    start = next(event for event in events if event["type"] == "http.response.start")
    body = b"".join(
        event.get("body", b"") for event in events if event["type"] == "http.response.body"
    )
    return start["status"], dict(start["headers"]), body


def test_function_get_implicitly_serves_head_with_same_metadata():
    app = Application()
    app.add_route("/hello", lambda request: "hello")
    status, headers, body = exchange(app, "/hello")
    h_status, h_headers, h_body = exchange(app, "/hello", "HEAD")
    assert (status, body) == (200, b"hello")
    assert h_status == status
    assert h_headers == headers
    assert h_body == b""


def test_method_mismatch_reports_allow_for_function_and_union_of_routes():
    app = Application()
    app.add_route("/item/{slug}", lambda request, slug: slug, methods=["GET"])
    app.add_route("/item/{slug}", lambda request, slug: slug, methods=["POST"])
    status, headers, _ = exchange(app, "/item/one", "DELETE")
    assert status == 405
    assert set(headers[b"allow"].decode().split(", ")) == {"GET", "HEAD", "POST"}
    assert exchange(app, "/missing", "DELETE")[0] == 404
    assert exchange(app, "/item/one/", "DELETE")[0] == 404


def test_controller_get_serves_head_and_missing_method_is_405():
    class Items(Controller):
        def get(self):
            return "items"

    app = Application()
    app.route("/items", Items)
    assert exchange(app, "/items", "GET")[2] == b"items"
    assert exchange(app, "/items", "HEAD") == (
        200,
        {b"content-type": b"text/plain; charset=utf-8", b"content-length": b"5"},
        b"",
    )
    status, headers, _ = exchange(app, "/items", "POST")
    assert (status, headers[b"allow"]) == (405, b"GET, HEAD")


def test_controller_explicit_head_takes_precedence_when_registered():
    class Items(Controller):
        def get(self):
            return "get"

        def head(self):
            return Response("different", headers={"X-Head": "explicit"})

    app = Application()
    app.route("/items", Items)
    status, headers, body = exchange(app, "/items", "HEAD")
    assert (status, headers[b"x-head"], body) == (200, b"explicit", b"")


def test_methods_preserve_registration_order_and_first_eligible_match():
    app = Application()
    app.add_route("/{name}", lambda request, name: "dynamic", methods=["POST"])
    app.add_route("/known", lambda request: "literal")
    assert exchange(app, "/known")[2] == b"literal"
    assert exchange(app, "/known", "POST")[2] == b"dynamic"


def test_static_405_reports_allow_header(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "asset.txt").write_text("ok")
    app = Application(static_directory=root)
    status, headers, body = exchange(app, "/statics/asset.txt", "POST")
    assert status == 405
    assert headers[b"allow"] == b"GET, HEAD"
    assert body == b"Method Not Allowed"
    assert exchange(app, "/statics/asset.txt", "HEAD")[2] == b""
