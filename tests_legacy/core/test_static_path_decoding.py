"""ASGI decoded-path contract for static assets."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora import Aurora


async def fetch(app, path):
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    await app({"type": "http", "method": "GET", "path": path,
               "headers": [], "query_string": b""}, receive, send)
    return messages


class StaticPathDecodingTests(unittest.TestCase):
    """The ASGI path is decoded exactly once, by the server."""

    def test_literal_percent_escape_in_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "report%20final.txt").write_bytes(b"literal")
            (root / "report final.txt").write_bytes(b"decoded")
            messages = asyncio.run(fetch(Aurora(statics_directory=root),
                                         "/statics/report%20final.txt"))
            self.assertEqual(messages[0]["status"], 200)
            self.assertEqual(messages[-1]["body"], b"literal")

    def test_percent_encoded_traversal_text_is_not_decoded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "%2e%2e.txt").write_bytes(b"safe")
            messages = asyncio.run(fetch(Aurora(statics_directory=root),
                                         "/statics/%2e%2e.txt"))
            self.assertEqual(messages[0]["status"], 200)
            self.assertEqual(messages[-1]["body"], b"safe")

    def test_actual_decoded_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            messages = asyncio.run(fetch(Aurora(statics_directory=tmp),
                                         "/statics/../secret.txt"))
            self.assertEqual(messages[0]["status"], 404)

    def test_decoded_unicode_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "café.txt").write_bytes(b"unicode")
            messages = asyncio.run(fetch(Aurora(statics_directory=root),
                                         "/statics/café.txt"))
            self.assertEqual(messages[0]["status"], 200)
            self.assertEqual(messages[-1]["body"], b"unicode")
