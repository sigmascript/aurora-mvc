import unittest

from aurora.core.request import Request
from aurora.core.route import Route
from aurora.core.router import Router


class RouteMethodValidationTests(unittest.TestCase):
    def test_none_defaults_to_get(self):
        route = Route("/items", lambda request: None)
        self.assertEqual(route.methods, {"GET"})

    def test_explicit_empty_methods_are_rejected(self):
        for methods in ([], (), set()):
            with self.subTest(methods=methods), self.assertRaisesRegex(ValueError, "cannot be empty"):
                Route("/items", lambda request: None, methods=methods)

    def test_scalar_and_non_iterable_methods_are_rejected(self):
        for methods in ("GET", b"GET", 42):
            with self.subTest(methods=methods), self.assertRaises(TypeError):
                Route("/items", lambda request: None, methods=methods)

    def test_invalid_method_values_are_rejected(self):
        for methods in ([None], [1], [""], ["GET POST"], ["GET\nInjected"], ["GÉT"]):
            with self.subTest(methods=methods), self.assertRaises(ValueError):
                Route("/items", lambda request: None, methods=methods)

    def test_case_normalization_and_duplicates(self):
        route = Route("/items", lambda request: None, methods=["get", "GET", "post"])
        self.assertEqual(route.methods, {"GET", "POST"})
        self.assertEqual(route.matches(Request({"path": "/items", "method": "POST"})), {})

    def test_failed_registration_does_not_modify_router(self):
        router = Router()
        with self.assertRaises(ValueError):
            router.add("/items", lambda request: None, methods=[], name="items")
        self.assertEqual(router.routes, [])
        self.assertEqual(router.named_routes, {})


if __name__ == "__main__":
    unittest.main()
