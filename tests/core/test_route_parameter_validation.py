"""Regression tests for invalid route parameter definitions."""

import unittest

from aurora.core.request import Request
from aurora.core.route import Route
from aurora.core.router import Router


class RouteParameterValidationTests(unittest.TestCase):
    def test_duplicate_parameter_names_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate route parameter name"):
            Route("/users/{id}/posts/{id}", lambda: None)

    def test_malformed_placeholders_are_rejected(self):
        for path in ("/users/{id", "/users/id}", "/users/{}", "/users/{9id}",
                     "/users/{first-name}", "/users/{{id}}"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, "Invalid route parameter placeholder"):
                    Route(path, lambda: None)

    def test_invalid_route_path_type_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "Route path must be a string"):
            Route(None, lambda: None)

    def test_valid_parameters_and_literal_brackets_are_unchanged(self):
        route = Route("/reports/[draft]/{report_id}", lambda: None)
        self.assertEqual(route.parameters, ["report_id"])
        self.assertEqual(route.matches(Request({"path": "/reports/[draft]/42"})), {"report_id": "42"})
        self.assertEqual(route.build_url(report_id="42"), "/reports/[draft]/42")

    def test_invalid_registration_does_not_change_router(self):
        router = Router()
        with self.assertRaises(ValueError):
            router.add("/users/{id}/{id}", lambda: None, name="broken")
        self.assertEqual(router.routes, [])
        self.assertEqual(router.named_routes, {})


if __name__ == "__main__":
    unittest.main()
