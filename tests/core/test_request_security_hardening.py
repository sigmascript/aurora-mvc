"""Phase 5.12: consistent scaffolding and safe static delivery."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora import Aurora
from aurora.cli import add_app, create_project


async def fetch(app, path, method="GET"):
    messages = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        messages.append(message)
    await app({"type": "http", "path": path, "method": method,
               "headers": [], "query_string": b""}, receive, send)
    return messages


class RequestSecurityHardeningTests(unittest.TestCase):
    def test_generated_controller_matches_app_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("demo", directory=Path(tmp), modular=True)
            add_app("news_feed", directory=root)
            self.assertTrue((root / "apps/news_feed/controllers/news_feed.py").exists())
            self.assertIn("controllers.news_feed import NewsFeed", (root / "app.py").read_text())

    def test_atomic_app_update_failure_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("demo", directory=Path(tmp), modular=True)
            before = (root / "app.py").read_bytes()
            with patch("aurora.cli.os.replace", side_effect=OSError("simulated failure")):
                with self.assertRaises(OSError):
                    add_app("news", directory=root)
            self.assertEqual((root / "app.py").read_bytes(), before)
            self.assertFalse((root / "apps/news").exists())
            self.assertFalse(list(root.glob(".aurora-app-*.tmp")))
            self.assertFalse(list((root / "apps").glob(".news-*")))

    def test_large_static_stream_and_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = b"abc123" * 40000
            (root / "large.bin").write_bytes(data)
            app = Aurora(statics_directory=root)
            messages = asyncio.run(fetch(app, "/statics/large.bin"))
            self.assertEqual(messages[0]["status"], 200)
            self.assertEqual(b"".join(m["body"] for m in messages[1:]), data)
            self.assertGreater(len(messages), 3)
            self.assertEqual(dict(messages[0]["headers"])[b"content-length"], str(len(data)).encode())
            head = asyncio.run(fetch(app, "/statics/large.bin", "HEAD"))
            self.assertEqual(head[0]["status"], 200)
            self.assertEqual(head[-1]["body"], b"")

    def test_static_hidden_and_encoded_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text("ok")
            (root / ".env").write_text("secret")
            app = Aurora(statics_directory=root)
            for path in ("/statics/.env", "/statics/%2eenv", "/statics/%2e%2e/ok.txt",
                         "/statics/%5cok.txt", "/statics/%00ok.txt"):
                with self.subTest(path=path):
                    self.assertEqual(asyncio.run(fetch(app, path))[0]["status"], 404)
