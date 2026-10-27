"""Alpha 3 modular starter: accessible views, isolation, and inert models."""
import json
from pathlib import Path

from aurora.cli import add_app, create_project


def test_modular_starter_is_complete_and_inert(tmp_path: Path) -> None:
    root = create_project("site", directory=tmp_path, modular=True)
    layout = (root / "views/layout.html").read_text(encoding="utf-8")
    home = (root / "apps/home/views/index.html").read_text(encoding="utf-8")
    blog = (root / "apps/blog/views/index.html").read_text(encoding="utf-8")
    css = (root / "statics/css/site.css").read_text(encoding="utf-8")
    assert 'lang="en"' in layout
    assert '{% include "navigation.html" %}' in layout
    assert '/statics/css/site.css' in layout
    assert '{% extends "layout.html" %}' in home
    assert '{% extends "layout.html" %}' in blog
    assert 'apps/home/controllers/home.py' in home
    assert 'focus-visible' in css
    assert 'nav a' in css
    assert (root / "models/__init__.py").read_text(encoding="utf-8") == ""
    assert json.loads((root / "aurora_models.json").read_text(encoding="utf-8")) == {"databases": {}}
    assert not (root / "data.sqlite3").exists()


def test_added_module_preserves_starter_and_empty_models(tmp_path: Path) -> None:
    root = create_project("site", directory=tmp_path, modular=True)
    before = (root / "views/layout.html").read_bytes()
    added = add_app("news", directory=root)
    assert (added / "models/__init__.py").read_text(encoding="utf-8") == ""
    assert '{% extends "layout.html" %}' in (added / "views/index.html").read_text(encoding="utf-8")
    assert (root / "views/layout.html").read_bytes() == before
    app = (root / "app.py").read_text(encoding="utf-8")
    assert 'app.route("/news", News)' in app
    assert '"news": ROOT / "apps" / "news" / "views"' in app
    assert not (root / "data.sqlite3").exists()
