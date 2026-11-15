"""F03 optional unprefixed locale-entry policy, without database or sessions."""
import asyncio
import json

import pytest

from aurora.core.application import Application


def exchange(app, path, *, method="GET", cookie=None, query=b"", scheme="http"):
    events = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        events.append(message)

    headers = [(b"cookie", cookie.encode())] if cookie else []
    asyncio.run(app({"type": "http", "path": path, "method": method,
                     "scheme": scheme, "headers": headers, "query_string": query},
                    receive, send))
    start = next(x for x in events if x["type"] == "http.response.start")
    return (start["status"], dict(start["headers"]),
            b"".join(x.get("body", b"") for x in events if x["type"] == "http.response.body"))


def localized(policy="explicit", cookie_name=None):
    app = Application(locales=["en", "ku"], default_locale="en",
                      locale_cookie_name=cookie_name, locale_entry_policy=policy)
    app.add_route("/admin/", lambda request: request.locale, methods=["GET"])
    app.add_route("/posts/{slug}", lambda request, slug: f"{request.locale}:{slug}")
    return app


def test_explicit_compatibility_has_no_redirect_or_cookie():
    app = localized(cookie_name="lang")
    assert exchange(app, "/admin/")[0] == 404
    assert exchange(app, "/en/admin/", cookie="lang=ku")[2] == b"en"
    assert b"set-cookie" not in exchange(app, "/ku/admin/")[1]


def test_default_policy_redirects_only_real_get_head_routes_and_keeps_query():
    app = localized("default")
    assert exchange(app, "/admin/", query=b"p=2&tag=a%20b")[:2] == (
        302, {b"content-type": b"text/plain; charset=utf-8", b"content-length": b"0",
              b"location": b"/en/admin/?p=2&tag=a%20b"})
    assert exchange(app, "/admin/", method="HEAD")[0] == 302
    assert exchange(app, "/admin/", method="POST")[0] == 404
    assert exchange(app, "/unknown")[0] == 404
    assert exchange(app, "/admin")[0] == 404
    assert exchange(app, "/ku/admin/")[2] == b"ku"
    assert exchange(app, "/posts/a")[1][b"location"] == b"/en/posts/a"


def test_default_policy_does_not_redirect_api_or_static_or_unknown_prefix():
    app = localized("default")
    app.add_route("/api/status", lambda request: "ok")
    assert exchange(app, "/api/status")[0] == 404
    assert exchange(app, "/fr/admin/")[0] == 404


def test_remembered_policy_uses_cookie_only_for_unprefixed_entry_and_sets_on_visit():
    app = localized("remembered", "lang")
    status, headers, _ = exchange(app, "/admin/", cookie="lang=ku")
    assert (status, headers[b"location"]) == (302, b"/ku/admin/")
    assert headers[b"cache-control"] == b"private, no-store"
    assert headers[b"vary"] == b"Cookie"
    assert exchange(app, "/admin/", cookie="lang=xx")[1][b"location"] == b"/en/admin/"
    status, headers, body = exchange(app, "/en/admin/", cookie="lang=ku")
    assert (status, body) == (200, b"en")
    assert b"lang=en" in headers[b"set-cookie"]
    assert b"httponly" in headers[b"set-cookie"].lower()
    assert headers[b"cache-control"] == b"private, no-store"
    assert b"set-cookie" not in exchange(app, "/en/admin/", cookie="lang=en")[1]
    assert b"secure" in exchange(app, "/ku/admin/", scheme="https")[1][b"set-cookie"].lower()
    assert b"set-cookie" not in exchange(app, "/en/unknown")[1]
    assert b"set-cookie" not in exchange(app, "/en/admin/", method="POST")[1]


def test_invalid_redirect_query_fails_closed():
    app = localized("remembered", "lang")
    for bad in (b"bad=%XX", b"bad=%FF", b"hello\r\n", b"foo#bar", b"\xff"):
        assert exchange(app, "/admin/", query=bad)[0] == 400


def test_policy_configuration_and_validation(tmp_path):
    with pytest.raises(ValueError):
        Application(locale_entry_policy="default")
    with pytest.raises(ValueError):
        Application(locales=["en"], default_locale="en", locale_entry_policy="remembered")
    with pytest.raises(ValueError):
        Application(locales=["en"], default_locale="en", locale_entry_policy="surprise")
    config = tmp_path / "aurora_config.json"
    config.write_text(json.dumps({"application": {"name": "Example", "locales": ["en", "ku"],
                                                 "default_locale": "en", "locale_cookie_name": "lang",
                                                 "locale_entry_policy": "remembered"}}))
    app = Application.from_config(config)
    assert app.locale_entry_policy == "remembered"
    config.write_text(json.dumps({"application": {"locale_entry_policy": "invalid"}}))
    with pytest.raises(ValueError):
        Application.from_config(config)
