"""D2-C9B: static assets cannot escape public root through filesystem links."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora.core.static_files import serve_static_file


class StaticSymlinkSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "public"
        self.root.mkdir()
        (self.root / "safe.txt").write_bytes(b"SAFE")
        (self.base / "private.txt").write_bytes(b"SECRET")

    def request(self, path, method="GET"):
        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(serve_static_file(
            {"path": "/static/" + path, "method": method},
            send, root=self.root, url_prefix="/static",
        ))
        return messages

    def test_regular_file(self):
        messages = self.request("safe.txt")
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(b"".join(m.get("body", b"") for m in messages[1:]), b"SAFE")

    def test_symlink_to_outside_is_rejected(self):
        try:
            (self.root / "escape.txt").symlink_to(self.base / "private.txt")
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        messages = self.request("escape.txt")
        self.assertEqual(messages[0]["status"], 404)
        self.assertNotIn(b"SECRET", repr(messages).encode())

    def test_symlink_directory_to_outside_is_rejected(self):
        try:
            (self.root / "escape").symlink_to(self.base, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation unavailable")
        self.assertEqual(self.request("escape/private.txt")[0]["status"], 404)

    def test_no_preopen_path_resolution_dependency(self):
        # The old implementation called Path.resolve before Path.open;
        # the new implementation opens relative to trusted directory handles.
        if os.name == "nt":
            self.skipTest("POSIX-specific handle traversal")
        with patch.object(Path, "resolve", side_effect=AssertionError("pre-open resolution")):
            self.assertEqual(self.request("safe.txt")[0]["status"], 200)

    def test_head_remains_bodyless(self):
        messages = self.request("safe.txt", "HEAD")
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(messages[-1]["body"], b"")
