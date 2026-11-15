"""F02 bounded multipart uploads: practical parsing and failure boundaries."""

import asyncio

import pytest

from aurora.core.http_exception import HTTPException
from aurora.core.request import Request, UploadedFile


def multipart(parts, boundary=b"aurora-boundary"):
    chunks = []
    for name, value, filename in parts:
        chunks.append(b"--" + boundary + b"\r\n")
        disposition = b'Content-Disposition: form-data; name="' + name + b'"'
        if filename is not None:
            disposition += b'; filename="' + filename + b'"'
        chunks.append(disposition + b"\r\n")
        if filename is not None:
            chunks.append(b"Content-Type: application/octet-stream\r\n")
        chunks.append(b"\r\n" + value + b"\r\n")
    chunks.append(b"--" + boundary + b"--\r\n")
    return b"".join(chunks)


def make_request(body, content_type="multipart/form-data; boundary=aurora-boundary", **options):
    received = False

    async def receive():
        nonlocal received
        assert not received, "Body stream read more than once"
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"method": "POST", "path": "/", "headers": [
        (b"content-type", content_type.encode())]}, receive, **options)


def test_fields_files_duplicate_names_are_in_memory_only():
    payload = multipart([
        (b"title", "first".encode(), None),
        (b"title", "café".encode(), None),
        (b"document", b"\x00\xff\x10", b"untrusted.txt"),
    ])
    req = make_request(payload)
    values = asyncio.run(req.form())
    assert values["title"] == "café"
    upload = values["document"]
    assert isinstance(upload, UploadedFile)
    assert (upload.filename, upload.content_type, upload.size, upload.content) == (
        "untrusted.txt", "application/octet-stream", 3, b"\x00\xff\x10")
    assert asyncio.run(req.body()) == payload
    assert asyncio.run(req.form()) == values


@pytest.mark.parametrize("data,content_type,status", [
    (b"garbage", "multipart/form-data; boundary=aurora-boundary", 400),
    (multipart([(b"x", b"ok", None)])[:-4], "multipart/form-data; boundary=aurora-boundary", 400),
    (b"", "multipart/form-data", 400),
    (multipart([(b"x", b"ok", None)]), "application/json", 415),
    (multipart([(b"x", b"\xff", None)]), "multipart/form-data; boundary=aurora-boundary", 400),
    (multipart([(b"x", b"a" * (4 * 1024 * 1024 + 1), b"test.bin")]), "multipart/form-data; boundary=aurora-boundary", 413),
    (multipart([(b"x", b"ok", None)] * 101), "multipart/form-data; boundary=aurora-boundary", 413),
])
def test_multipart_invalid_or_bounded(data, content_type, status):
    req = make_request(data, content_type)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(req.form())
    assert caught.value.status_code == status


def test_request_size_limit_applies_even_when_body_was_cached_first():
    payload = multipart([(b"file", b"a" * 400, b"file.bin")])
    req = make_request(payload, max_body_bytes=200)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(req.form())
    assert caught.value.status_code == 413
    req = make_request(payload)
    asyncio.run(req.body())
    assert asyncio.run(req.form())["file"].size == 400


def test_existing_urlencoded_form_behavior_unchanged():
    req = make_request(b"tag=first&tag=last&empty=", "application/x-www-form-urlencoded")
    assert asyncio.run(req.form()) == {"tag": "last", "empty": ""}


def test_multipart_through_real_asgi_route():
    from aurora.core.application import Application

    app = Application()

    async def upload(request):
        values = await request.form()
        return str(values["file"].size)

    app.add_route("/upload", upload, methods=["POST"])

    async def exchange(payload):
        messages = []

        async def receive():
            return {"type": "http.request", "body": payload, "more_body": False}

        async def send(message):
            messages.append(message)

        await app({"type": "http", "method": "POST", "path": "/upload",
                   "headers": [(b"content-type", b"multipart/form-data; boundary=aurora-boundary")],
                   "query_string": b""}, receive, send)
        return messages

    successful = asyncio.run(exchange(multipart([(b"file", b"data", b"a.txt")])))
    assert successful[0]["status"] == 200
    assert successful[1]["body"] == b"4"

    malformed = asyncio.run(exchange(b"not multipart"))
    assert malformed[0]["status"] == 400
