"""Controller inheritance and named-action contract regression tests."""

import asyncio
import unittest

from aurora.core.controller import Controller
from aurora.core.http_exception import HTTPException
from aurora.core.request import Request


class ControllerContractTests(unittest.TestCase):
    def test_inherited_http_methods_are_discovered_and_dispatched(self):
        class Parent(Controller):
            def get(self):
                return "inherited"

        class Child(Parent):
            def post(self):
                return "own"

        self.assertEqual(Child.http_methods(), ["GET", "POST"])
        self.assertEqual(Child.dispatch(Request({"method": "GET"})), "inherited")
        self.assertEqual(Child.dispatch(Request({"method": "POST"})), "own")

    def test_non_callable_http_attribute_is_not_discovered(self):
        class Resource(Controller):
            get = None

        self.assertEqual(Resource.http_methods(), [])
        with self.assertRaises(HTTPException) as caught:
            Resource.dispatch(Request({"method": "GET"}))
        self.assertEqual(caught.exception.status_code, 405)

    def test_named_action_supports_sync_and_async_methods(self):
        class Resource(Controller):
            def show(self, id):
                return f"sync:{id}"

            async def save(self, id):
                return f"async:{id}"

        request = Request({"method": "GET"})
        self.assertEqual(Resource.action("show")(request, id="3"), "sync:3")
        self.assertEqual(asyncio.run(Resource.action("save")(request, id="4")), "async:4")

    def test_action_rejects_invalid_names_at_creation(self):
        class Resource(Controller):
            pass

        for name in (None, "", "  ", 42):
            with self.subTest(name=name), self.assertRaises(ValueError):
                Resource.action(name)

    def test_unknown_named_action_keeps_lazy_lookup_behavior(self):
        class Resource(Controller):
            pass

        handler = Resource.action("missing")
        with self.assertRaises(AttributeError):
            handler(Request({"method": "GET"}))
