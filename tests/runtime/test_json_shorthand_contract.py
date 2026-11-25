"""M7-11: direct JSON responses preserve explicit HTTP semantics."""

import asyncio
import json
from datetime import date

import pytest

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.response import JSONResponse, Response
from aurora.database.models import ModelResult, ModelRow


def exchange(app, path="/", method="GET"):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(
        app(
            {"type": "http", "path": path, "method": method, "headers": [], "query_string": b""},
            receive,
            send,
        )
    )
    return (messages[0]["status"], dict(messages[0]["headers"]), messages[1]["body"])


def test_plain_mapping_and_list_return_json_from_controller_and_route():
    app = Application()

    class Items(Controller):
        def get(self):
            return {"title": "سلام", "count": 2}

    app.route("/controller", Items)
    app.add_route("/route", lambda request: [{"id": 1}, {"id": 2}])
    status, headers, body = exchange(app, "/controller")
    assert status == 200
    assert headers[b"content-type"] == b"application/json; charset=utf-8"
    assert json.loads(body) == {"title": "سلام", "count": 2}
    assert json.loads(exchange(app, "/route")[2]) == [{"id": 1}, {"id": 2}]


def test_model_row_result_and_empty_join_are_materialized_json():
    app = Application()
    result = ModelResult([{"id": 1, "title": "Ready"}])
    app.add_route("/result", lambda request: result)
    app.add_route("/row", lambda request: ModelRow({"title": "Ready"}))
    app.add_route("/empty", lambda request: ModelResult([]))
    assert json.loads(exchange(app, "/result")[2]) == [{"id": 1, "title": "Ready"}]
    assert json.loads(exchange(app, "/row")[2]) == {"title": "Ready"}
    assert json.loads(exchange(app, "/empty")[2]) == []


def test_data_status_tuple_and_explicit_response_precedence():
    app = Application()
    app.add_route("/created", lambda request: ({"id": 4}, 201), methods=["POST"])
    app.add_route("/explicit", lambda request: JSONResponse({"id": 4}, 202))
    app.add_route("/text", lambda request: Response("hello", status_code=203))
    status, headers, body = exchange(app, "/created", "POST")
    assert (status, json.loads(body)) == (201, {"id": 4})
    assert headers[b"content-type"] == b"application/json; charset=utf-8"
    assert exchange(app, "/explicit")[0] == 202
    assert exchange(app, "/text") == (
        203,
        {b"content-type": b"text/plain; charset=utf-8", b"content-length": b"5"},
        b"hello",
    )


def test_json_serializer_remains_strict_for_unsupported_values():
    app = Application()
    with pytest.raises(TypeError):
        asyncio.run(app._response_from_result(None, {"day": date(2026, 11, 22)}))
    with pytest.raises(ValueError, match="status"):
        asyncio.run(app._response_from_result(None, ({"ok": True}, 700)))
