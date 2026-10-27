"""Phase 5.9: readable, inherited starter views."""
from pathlib import Path
import tempfile
import unittest

from aurora.cli import add_app, create_project
from aurora.view import View


class TestViewTemplates(unittest.TestCase):
    def test_mvc_view_extends_generated_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project("site", directory=Path(temp), mvc=True)
            layout = (root / "views/layout.html").read_text(encoding="utf-8")
            home = (root / "views/home.html").read_text(encoding="utf-8")
            self.assertIn('{% block content %}', layout)
            self.assertIn('/statics/css/site.css', layout)
            self.assertIn('{% extends "layout.html" %}', home)
            self.assertIn('<h1 id="welcome-title">{{ title }}</h1>', home)
            self.assertIn('aria-labelledby="welcome-title"', home)
            self.assertIn('{% block title %}', home)

    def test_modular_views_are_readable_and_inherit_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project("site", directory=Path(temp), modular=True)
            layout = (root / "views/layout.html").read_text(encoding="utf-8")
            self.assertIn('\n    <main>', layout)
            for path in ("apps/home/views/index.html", "apps/blog/views/index.html"):
                view = (root / path).read_text(encoding="utf-8")
                self.assertIn('{% extends "layout.html" %}', view)
                self.assertIn('\n    <h1>', view)

    def test_new_app_view_is_readable_and_inherits_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project("site", directory=Path(temp), modular=True)
            add_app("news", directory=root)
            view = (root / "apps/news/views/index.html").read_text(encoding="utf-8")
            self.assertIn('{% extends "layout.html" %}', view)
            self.assertIn('\n    <p>', view)
