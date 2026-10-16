"""Regression tests for static-file delivery after responsibility extraction."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora import Aurora


async def fetch(app, path, method="GET"):
    """Return ASGI messages from one HTTP request."""
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app(
        {
            "type": "http",
            "path": path,
            "method": method,
            "headers": [],
            "query_string": b"",
        },
        receive,
        send,
    )
    return messages


class StaticFilesExtractionTests(unittest.TestCase):
    """Preserve HTTP status, response bodies, and safety boundaries."""

    def test_missing_file_returns_404(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Aurora(statics_directory=tmp)
            messages = asyncio.run(fetch(app, "/statics/missing.txt"))

            self.assertEqual(messages[0]["status"], 404)
            self.assertEqual(messages[-1]["body"], b"Not Found")

    def test_unsupported_method_returns_405_for_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "asset.txt").write_text("hello", encoding="utf-8")
            app = Aurora(statics_directory=root)
            messages = asyncio.run(fetch(app, "/statics/asset.txt", "POST"))

            self.assertEqual(messages[0]["status"], 405)
            self.assertEqual(messages[-1]["body"], b"Method Not Allowed")

    def test_head_has_content_length_but_no_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "asset.txt").write_bytes(b"hello")
            app = Aurora(statics_directory=root)
            messages = asyncio.run(fetch(app, "/statics/asset.txt", "HEAD"))

            self.assertEqual(messages[0]["status"], 200)
            self.assertEqual(dict(messages[0]["headers"])[b"content-length"], b"5")
            self.assertEqual(messages[-1]["body"], b"")

    def test_hidden_and_traversal_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "asset.txt").write_text("hello", encoding="utf-8")
            app = Aurora(statics_directory=root)

            for path in ("/statics/.env", "/statics/%2e%2e/asset.txt", "/statics/%5casset.txt"):
                with self.subTest(path=path):
                    messages = asyncio.run(fetch(app, path))
                    self.assertEqual(messages[0]["status"], 404)
