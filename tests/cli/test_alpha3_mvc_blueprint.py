"""Alpha 3 MVC starter contract: readable, runnable, and isolated."""
from pathlib import Path

from aurora.cli import create_project


def test_mvc_starter_contains_accessible_view_and_styles(tmp_path: Path) -> None:
    root = create_project("example", directory=tmp_path, mvc=True)
    layout = (root / "views/layout.html").read_text(encoding="utf-8")
    home = (root / "views/home.html").read_text(encoding="utf-8")
    css = (root / "statics/css/site.css").read_text(encoding="utf-8")
    assert 'lang="en"' in layout
    assert 'id="main-content"' in layout
    assert 'href="/statics/css/site.css"' in layout
    assert '{% extends "layout.html" %}' in home
    assert 'controllers/Home.py' in home
    assert 'views/home.html' in home
    assert '.action-link' in css
    assert 'focus-visible' in css
    assert (root / "models/__init__.py").read_text(encoding="utf-8") == ""
    assert not list(root.rglob("*.sqlite3"))


def test_mvc_styles_do_not_change_modular_starter(tmp_path: Path) -> None:
    mvc = create_project("mvc_example", directory=tmp_path, mvc=True)
    modular = create_project("modular_example", directory=tmp_path, modular=True)
    assert (mvc / "statics/css/site.css").read_text(encoding="utf-8") != (modular / "statics/css/site.css").read_text(encoding="utf-8")
    assert 'nav a' in (modular / "statics/css/site.css").read_text(encoding="utf-8")
    assert (modular / "apps/home/views/index.html").exists()
