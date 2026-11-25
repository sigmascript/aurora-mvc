"""Essential framework security contracts, without legacy compatibility scaffolding."""

import asyncio

import pytest

from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.core.session import Session
from aurora.security import (
    csrf_token,
    hash_password,
    hash_password_async,
    protected_form,
    validate_csrf,
    verify_password,
    verify_password_async,
)


def request(method="POST", body=""):
    async def receive():
        return {"type": "http.request", "body": body.encode(), "more_body": False}

    req = Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        },
        receive,
    )
    req._session = Session()
    return req


def test_csrf_token_is_session_bound_and_validation_does_not_create_one():
    first = request()
    assert not first.session.changed
    with pytest.raises(HTTPException) as missing:
        validate_csrf(first, "absent")
    assert missing.value.status_code == 403
    assert not first.session.changed

    token = csrf_token(first)
    assert token == csrf_token(first)
    assert first.session.changed
    assert validate_csrf(first, token)

    for candidate in (None, "", "bad", 123, "é", "x" * 300):
        with pytest.raises(HTTPException) as invalid:
            validate_csrf(first, candidate)
        assert invalid.value.status_code == 403

    with pytest.raises(HTTPException) as other:
        validate_csrf(request(), token)
    assert other.value.status_code == 403

    with pytest.raises(HTTPException) as safe_method:
        validate_csrf(request("GET"), token)
    assert safe_method.value.status_code == 405


def test_protected_form_and_session_rotation():
    async def journey():
        first = request()
        token = csrf_token(first)
        form = request(body=f"csrf_token={token}&title=hello")
        form._session = Session({"_aurora_csrf_token": token})
        assert (await protected_form(form))["title"] == "hello"

        missing = request(body="title=hello")
        missing._session = Session({"_aurora_csrf_token": token})
        with pytest.raises(HTTPException) as rejected:
            await protected_form(missing)
        assert rejected.value.status_code == 403

        await first.session.regenerate()
        new_token = csrf_token(first)
        assert new_token != token
        with pytest.raises(HTTPException):
            validate_csrf(first, token)
        assert validate_csrf(first, new_token)

    asyncio.run(journey())


def test_password_hashing_refuses_wrong_and_malformed_values():
    stored = hash_password("a secure example passphrase")
    assert verify_password("a secure example passphrase", stored)
    assert not verify_password("wrong", stored)
    assert stored != hash_password("a secure example passphrase")
    for invalid in ("", "x" * 129, None, b"bytes"):
        with pytest.raises(ValueError):
            hash_password(invalid)
    for malformed in (None, "", "plaintext", "scrypt$999999999$8$1$ab$cd", "x" * 100000):
        assert not verify_password("a secure example passphrase", malformed)


def test_async_password_helpers():
    async def journey():
        stored = await hash_password_async("unicode passphrase سڵاو")
        assert await verify_password_async("unicode passphrase سڵاو", stored)
        assert not await verify_password_async("wrong", stored)

    asyncio.run(journey())
