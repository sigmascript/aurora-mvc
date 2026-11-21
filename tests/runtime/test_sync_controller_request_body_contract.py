"""Synchronous controller body parsing without blocking the ASGI receive loop."""

import asyncio
import threading

import pytest

from aurora.core.application import Application
from aurora.core.controller import Controller


def exchange(app, path, body, content_type, *, method="POST", disconnect=False):
    messages = []
    reads = 0
    thread = threading.get_ident()

    async def receive():
        nonlocal reads
        assert threading.get_ident() == thread, "ASGI receive must remain on the loop thread"
        reads += 1
        if disconnect:
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    async def run():
        await app({"type": "http", "method": method, "path": path,
                   "headers": [(b"content-type", content_type.encode())],
                   "query_string": b""}, receive, send)

    asyncio.run(run())
    if not messages:
        return None, b"", reads

    return messages[0]["status"], messages[1].get("body", b""), reads


def test_sync_form_and_json_return_parsed_values_directly():
    app = Application()

    class Notes(Controller):
        def post(self):
            values = self.request.form()
            return values["title"]

    class Api(Controller):
        def post(self):
            return str(self.request.json()["quantity"])

    app.route("/notes", Notes)
    app.route("/api", Api)
    assert exchange(app, "/notes", b"title=hello+world", "application/x-www-form-urlencoded") == (
        200, b"hello world", 1)
    assert exchange(app, "/api", b'{"quantity": 4}', "application/json") == (200, b"4", 1)


def test_async_parsers_remain_awaitable_and_middleware_can_read_first():
    app = Application()

    @app.add_middleware
    async def read_before_dispatch(request, call_next):
        assert (await request.body()) == b'{"title":"hello"}'
        return await call_next(request)

    class Notes(Controller):
        async def post(self):
            return (await self.request.json())["title"]

    app.route("/notes", Notes)
    assert exchange(app, "/notes", b'{"title":"hello"}', "application/json") == (200, b"hello", 1)


def test_named_sync_controller_action_and_default_get_do_not_read_body():
    app = Application()

    class Notes(Controller):
        def show(self):
            return "hello"

        def save(self):
            return self.request.form()["title"]

    app.add_route("/show", Notes.action("show"))
    app.add_route("/save", Notes.action("save"), methods=["POST"])
    assert exchange(app, "/show", b"", "application/x-www-form-urlencoded", method="GET") == (
        200, b"hello", 0)
    assert exchange(app, "/save", b"title=worked", "application/x-www-form-urlencoded") == (
        200, b"worked", 1)


@pytest.mark.parametrize("body,media,status", [
    (b"broken", "application/json", 400),
    (b"a=1", "text/plain", 415),
])
def test_sync_controller_keeps_parser_error_contract(body, media, status):
    app = Application()

    class Notes(Controller):
        def post(self):
            return str(self.request.form()["a"] if media != "application/json" else self.request.json())

    app.route("/notes", Notes)
    actual, _, reads = exchange(app, "/notes", body, media)
    assert actual == status
    assert reads == 1


def test_sync_body_limit_and_disconnect_are_handled_before_controller():
    app = Application(max_request_body_bytes=3)
    invoked = []

    class Notes(Controller):
        def post(self):
            invoked.append(True)
            return self.request.form()["x"]

    app.route("/notes", Notes)
    status, _, _ = exchange(app, "/notes", b"x=long", "application/x-www-form-urlencoded")
    assert status == 413
    assert invoked == [True]  # Parsing is on-demand inside the controller.

    status, _, _ = exchange(app, "/notes", b"", "application/x-www-form-urlencoded", disconnect=True)
    assert status is None  # Client disconnected before a response could be sent.
    assert invoked == [True, True]


def test_sync_controller_does_not_execute_on_asgi_loop_thread():
    app = Application()
    loop_thread = threading.get_ident()

    class Notes(Controller):
        def post(self):
            assert threading.get_ident() != loop_thread
            return self.request.form()["x"]

    app.route("/notes", Notes)
    assert exchange(app, "/notes", b"x=y", "application/x-www-form-urlencoded")[:2] == (200, b"y")


def test_sync_post_that_does_not_parse_body_never_consumes_receive():
    app = Application()

    class Ping(Controller):
        def post(self):
            return "pong"

    app.route("/ping", Ping)
    assert exchange(app, "/ping", b"anything", "text/plain") == (200, b"pong", 0)


def test_multipart_upload_remains_bounded_inside_sync_controller():
    app = Application()

    class Upload(Controller):
        def post(self):
            return str(self.request.form()["file"].size)

    app.route("/upload", Upload)
    boundary = b"test"
    body = (b"--test\r\nContent-Disposition: form-data; name=\"file\"; "
            b"filename=\"a.bin\"\r\nContent-Type: application/octet-stream\r\n\r\n"
            b"abc\r\n--test--\r\n")
    assert exchange(app, "/upload", body, "multipart/form-data; boundary=test") == (200, b"3", 1)


def test_sync_action_does_not_block_concurrent_async_controller():
    app = Application()

    class Slow(Controller):
        def post(self):
            return self.request.json()["name"]

    class Fast(Controller):
        async def get(self):
            return "fast"

    app.route("/slow", Slow)
    app.route("/fast", Fast)
    messages = []

    async def slow_receive():
        await asyncio.sleep(0.04)
        return {"type": "http.request", "body": b'{"name":"slow"}', "more_body": False}

    async def fast_receive():
        raise AssertionError("GET should not read body")

    async def run():
        async def call(path, method, receive):
            sent = []

            async def send(message):
                sent.append(message)

            await app({"type": "http", "method": method, "path": path,
                       "headers": [(b"content-type", b"application/json")],
                       "query_string": b""}, receive, send)
            messages.append((path, sent[0]["status"], sent[1]["body"]))

        await asyncio.gather(
            call("/slow", "POST", slow_receive),
            call("/fast", "GET", fast_receive),
        )

    asyncio.run(run())
    assert sorted(messages) == [("/fast", 200, b"fast"), ("/slow", 200, b"slow")]
