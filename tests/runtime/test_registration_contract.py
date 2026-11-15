"""F03 registration safety: explicit public actions and proven route shadowing."""

import warnings

import pytest

from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.request import Request


def test_named_actions_validate_at_registration_and_allow_inheritance():
    class Base(Controller):
        def show(self, slug):
            return slug

        def _internal(self):
            return "secret"

    class Child(Base):
        disabled = None

    app = Application()
    app.add_route("/article/{slug}", Child.action("show"))
    route, params = app.router.resolve(Request({"method": "GET", "path": "/article/a"}))
    assert route.handler(Request({"method": "GET"}), **params) == "a"

    for name in ("missing", "_internal", "__class__", "disabled", "show.more", "getattr"):
        with pytest.raises(ValueError, match="action"):
            Child.action(name)


def test_duplicate_method_registration_warns_but_preserves_first_route():
    app = Application()
    app.add_route("/posts", lambda request: "first", methods=["GET"])
    with pytest.warns(UserWarning, match="unreachable.*duplicate path.*registration order"):
        app.add_route("/posts", lambda request: "second", methods=["HEAD"])
    route, _ = app.router.resolve(Request({"method": "HEAD", "path": "/posts"}))
    assert route.handler(Request({"method": "HEAD"})) == "first"


def test_literal_shadowing_warns_but_distinct_methods_do_not():
    app = Application()
    app.add_route("/articles/{slug}", lambda request, slug: slug, methods=["GET"])
    with pytest.warns(UserWarning, match="earlier parameter route shadows later literal path"):
        app.add_route("/articles/create", lambda request: "create", methods=["GET"])

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app.add_route("/articles/create", lambda request: "created", methods=["POST"])
    assert caught == []
    route, _ = app.router.resolve(Request({"method": "GET", "path": "/articles/create"}))
    assert route.path == "/articles/{slug}"
    route, _ = app.router.resolve(Request({"method": "POST", "path": "/articles/create"}))
    assert route.path == "/articles/create"


def test_literal_first_does_not_warn_and_nonprovable_overlap_is_ignored():
    app = Application()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        app.add_route("/articles/create", lambda request: "literal")
        app.add_route("/articles/{slug}", lambda request, slug: slug)
        app.add_route("/{collection}/{slug}", lambda request, collection, slug: slug)
    assert caught == []
