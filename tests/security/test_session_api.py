import unittest

from aurora.core.request import Request
from aurora.core.session import Session


class TestSessionAPI(unittest.IsolatedAsyncioTestCase):
    def test_dictionary_operations_and_change_tracking(self):
        session = Session({"theme": "light"})
        self.assertFalse(session.changed)
        self.assertEqual(session.get("theme"), "light")
        session["theme"] = "dark"
        session["user_id"] = 5
        self.assertIn("user_id", session)
        self.assertEqual(session.pop("theme"), "dark")
        self.assertTrue(session.changed)
        session.clear()
        self.assertEqual(dict(session), {})

    def test_rejects_unserializable_values_and_keys(self):
        session = Session()
        with self.assertRaises(TypeError):
            session[3] = "value"
        with self.assertRaises(TypeError):
            session["object"] = object()
        with self.assertRaises(TypeError):
            session["nan"] = float("nan")

    def test_request_isolation_and_unconfigured_access(self):
        first = Request({"type": "http"})
        second = Request({"type": "http"})
        with self.assertRaises(RuntimeError):
            _ = first.session
        first._session = Session()
        second._session = Session()
        first.session["user_id"] = 12
        self.assertIsNone(second.session.get("user_id"))

    async def test_regeneration_preserves_data_and_requests_rotation(self):
        session = Session({"user_id": 12})
        await session.regenerate()
        self.assertTrue(session.rotation_requested)
        self.assertEqual(session["user_id"], 12)

    async def test_invalidation_is_terminal(self):
        session = Session({"user_id": 12})
        await session.regenerate()
        await session.invalidate()
        self.assertTrue(session.invalidated)
        self.assertFalse(session.rotation_requested)
        with self.assertRaises(RuntimeError):
            session["user_id"] = 13
        with self.assertRaises(RuntimeError):
            session.get("user_id")
        with self.assertRaises(RuntimeError):
            await session.regenerate()
