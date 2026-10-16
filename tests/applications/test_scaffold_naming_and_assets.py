"""Phase 5.7 regression tests for scaffold naming and public assets."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora import Aurora
from aurora.cli import add_app, create_project, main


async def request(app, path, method="GET"):
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await app({"type": "http", "path": path, "method": method, "headers": [], "query_string": b""}, receive, send)
    return messages[0]["status"], messages[-1]["body"], dict(messages[0]["headers"])


class ScaffoldNamingAndAssetsTests(unittest.TestCase):
    def test_app_name_validation_and_pascalcase(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            for invalid in ("2news", "my-app", "News", "_news", "news__feed", "class"):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    add_app(invalid, directory=root)
            add_app("news_feed", directory=root)
            source = (root / "apps/news_feed/controllers/news_feed.py").read_text()
            self.assertIn("class NewsFeed(Controller)", source)
            self.assertIn('app.route("/news_feed", NewsFeed)', (root / "app.py").read_text())

    def test_production_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            with patch("aurora.cli.Path.cwd", return_value=root), patch.dict(os.environ, {"AURORA_ENV": "production"}):
                with self.assertRaises(SystemExit) as caught:
                    main(["create-app", "news"])
            self.assertEqual(caught.exception.code, 1)
            self.assertFalse((root / "apps/news").exists())

    def test_static_assets_and_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "site.css").write_text("body { color: red; }")
            (root / ".secret").write_text("hidden")
            app = Aurora(static_directory=root)
            status, body, headers = asyncio.run(request(app, "/statics/site.css"))
            self.assertEqual(status, 200)
            self.assertEqual(body, b"body { color: red; }")
            self.assertEqual(headers[b"content-type"], b"text/css")
            status, body, _ = asyncio.run(request(app, "/statics/site.css", "HEAD"))
            self.assertEqual((status, body), (200, b""))
            for path in ("/statics/.secret", "/statics/../other", "/statics/%2e%2e/other"):
                with self.subTest(path=path):
                    self.assertEqual(asyncio.run(request(app, path))[0], 404)

    def test_scaffolds_include_static(self):
        with tempfile.TemporaryDirectory() as tmp:
            for style in ("mvc", "modular"):
                root = create_project(style, directory=Path(tmp), **{style: True})
                self.assertTrue((root / "statics").is_dir())
                self.assertIn("statics_directory=", (root / "app.py").read_text())

    def test_reject_case_insensitive_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            (root / "apps" / "News").mkdir()
            with self.assertRaises(FileExistsError):
                add_app("news", directory=root)

    def test_custom_statics_directory_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "site.css").write_text("ok", encoding="utf-8")
            app = Aurora(statics_directory=root, static_url="/assets")
            self.assertEqual(asyncio.run(request(app, "/assets/site.css"))[0], 200)
            self.assertEqual(asyncio.run(request(app, "/statics/site.css"))[0], 404)

    def test_legacy_directory_keyword_and_conflicting_keywords(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "site.css").write_text("ok", encoding="utf-8")
            app = Aurora(static_directory=root, static_url="/static")
            self.assertEqual(asyncio.run(request(app, "/static/site.css"))[0], 200)
            with self.assertRaises(ValueError):
                Aurora(static_directory=root, statics_directory=root)
