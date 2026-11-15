"""F02 request query parameters and ordinary binary response behavior."""

import asyncio

import pytest

from aurora.core.application import Application
from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.core.response import Response


def test_readonly_query_params_keep_string_values_and_repeated_order():
    request = Request({"path": "/", "query_string": b"tag=a&tag=b&empty=&page=02&q=caf%C3%A9"})
    params = request.query_params
    assert params["tag"] == "b"
    assert params.get("page") == "02"
    assert params.get("missing") is None
    assert params.getall("tag") == ("a", "b")
    assert params.getall("missing") == ()
    assert params["empty"] == ""
    assert params["q"] == "café"
    assert dict(params)["page"] == "02"
    with pytest.raises(TypeError):
        params["page"] = "3"
    assert request.query_params is params
    assert request.query_string == "tag=a&tag=b&empty=&page=02&q=caf%C3%A9"


@pytest.mark.parametrize("raw", [b"q=%FF", b"x=%", b"x=%GG", b"\xff", b"a=1&" * 1001])
def test_invalid_or_oversized_query_fails_with_400(raw):
    request = Request({"path": "/", "query_string": raw})
    with pytest.raises(HTTPException) as caught:
        request.query_params
    assert caught.value.status_code == 400


def test_binary_response_preserves_exact_bytes_and_metadata():
    payload = b"\x00\xff\x80\x01"
    response = Response(payload, content_type="application/octet-stream", status_code=201)
    start, body = response.prepare()
    assert start["status"] == 201
    assert dict(start["headers"])[b"content-length"] == b"4"
    assert dict(start["headers"])[b"content-type"] == b"application/octet-stream"
    assert body["body"] == payload
    assert Response(payload, status_code=204).prepare()[1]["body"] == b""
    with pytest.raises(TypeError):
        Response(123).prepare()


def test_binary_route_head_has_get_length_but_no_body():
    app = Application()
    app.add_route("/binary", lambda req: Response(b"\x00\xff", content_type="image/png"))

    async def exchange(method):
        events = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(event):
            events.append(event)

        await app({"type": "http", "path": "/binary", "method": method,
                   "headers": [], "query_string": b""}, receive, send)
        return events

    get = asyncio.run(exchange("GET"))
    head = asyncio.run(exchange("HEAD"))
    assert get[0] == head[0]
    assert get[1]["body"] == b"\x00\xff"
    assert head[1]["body"] == b""
