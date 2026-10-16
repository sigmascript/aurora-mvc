"""D2-C8: static-file ASGI response boundary regression tests."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora.core.static_files import serve_static_file


class StaticASGIBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        (self.root / "example.txt").write_bytes(b"hello")

    def invoke(self, method="GET", *, send=None):
        messages = []

        async def capture(message):
            messages.append(message)
            if send is not None:
                await send(message)

        async def run():
            await serve_static_file(
                {"path": "/static/example.txt", "method": method},
                capture, root=self.root, url_prefix="/static",
            )

        return messages, run

    def test_head_sends_no_body_and_preserves_file_length(self):
        messages, run = self.invoke("HEAD")
        asyncio.run(run())
        self.assertEqual([m["type"] for m in messages],
                         ["http.response.start", "http.response.body"])
        self.assertEqual(messages[0]["status"], 200)
        self.assertIn((b"content-length", b"5"), messages[0]["headers"])
        self.assertEqual(messages[1]["body"], b"")

    def test_unsupported_method_returns_405(self):
        messages, run = self.invoke("POST")
        asyncio.run(run())
        self.assertEqual(messages[0]["status"], 405)

    def test_send_failure_after_start_never_retries_response(self):
        async def fail_on_body(message):
            if message["type"] == "http.response.body":
                raise OSError("connection lost")

        messages, run = self.invoke(send=fail_on_body)
        with self.assertRaisesRegex(OSError, "connection lost"):
            asyncio.run(run())
        self.assertEqual([m["type"] for m in messages].count("http.response.start"), 1)

    def test_send_failure_on_start_never_retries_response(self):
        async def fail_on_start(message):
            if message["type"] == "http.response.start":
                raise OSError("connection lost")

        messages, run = self.invoke(send=fail_on_start)
        with self.assertRaisesRegex(OSError, "connection lost"):
            asyncio.run(run())
        self.assertEqual(len(messages), 1)

    def test_open_failure_returns_404_before_start(self):
        with patch("aurora.core.static_files._open_static_file", side_effect=OSError("file vanished")):
            messages, run = self.invoke()
            asyncio.run(run())
        self.assertEqual(messages[0]["status"], 404)
        self.assertEqual([m["type"] for m in messages].count("http.response.start"), 1)


if __name__ == "__main__":
    unittest.main()
