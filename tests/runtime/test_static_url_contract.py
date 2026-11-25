"""F03 approved static URL generation: safe, context-free, and optional."""

import pytest

from aurora import Aurora, View


def test_static_urls_are_encoded_and_use_configured_prefix(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    app = Aurora(statics_directory=root, static_url="/assets")
    assert app.static_url_for("css/site.css") == "/assets/css/site.css"
    assert app.static_url_for("blog/café menu.png") == "/assets/blog/caf%C3%A9%20menu.png"
    assert app.static_url_for("no-file.svg") == "/assets/no-file.svg"
    assert Aurora().static_url_for("css/site.css") == "/statics/css/site.css"


def test_static_url_available_to_jinja_without_request_or_session(tmp_path):
    views = tmp_path / "views"
    views.mkdir()
    (views / "index.html").write_text('<link href="{{ static_url("css/site.css") }}">')
    app = Aurora(views_directory=views, static_url="/public")
    assert app.renderer.render(View("index")) == '<link href="/public/css/site.css">'
    assert "request" not in app.renderer.environment.globals
    assert "session" not in app.renderer.environment.globals


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/secret",
        "../secret",
        "images/../secret",
        "./site.css",
        "a//b",
        "a/./b",
        "a/.git/config",
        "a\\b",
        "a%2fb",
        "a%252fb",
        "a?x=1",
        "a#x",
        "C:drive",
        "bad\x00name",
        "bad\nname",
        "bad\x7fname",
        None,
        13,
    ],
)
def test_static_url_rejects_unsafe_or_ambiguous_paths(path):
    app = Aurora()
    with pytest.raises((ValueError, TypeError)):
        app.static_url_for(path)
