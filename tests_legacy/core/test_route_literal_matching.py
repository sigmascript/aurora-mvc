import unittest

from aurora.core.request import Request
from aurora.core.route import Route
from aurora.core.router import Router


class RouteLiteralMatchingTests(unittest.TestCase):
    def test_period_in_literal_path_is_not_wildcard(self):
        route = Route("/reports/v1.0", lambda request: None)

        self.assertEqual(route.matches(Request({"path": "/reports/v1.0"})), {})
        self.assertIsNone(route.matches(Request({"path": "/reports/v1X0"})))

    def test_regex_metacharacters_are_literal(self):
        route = Route("/docs/(draft)+[1]", lambda request: None)

        self.assertEqual(route.matches(Request({"path": "/docs/(draft)+[1]"})), {})
        self.assertIsNone(route.matches(Request({"path": "/docs/draft1"})))

    def test_parameter_matching_survives_literal_escaping(self):
        route = Route("/v1.0/users/{user_id}", lambda request: None)

        self.assertEqual(
            route.matches(Request({"path": "/v1.0/users/42"})),
            {"user_id": "42"},
        )
        self.assertIsNone(route.matches(Request({"path": "/v1X0/users/42"})))
        self.assertIsNone(route.matches(Request({"path": "/v1.0/users/42/posts"})))

    def test_named_route_builds_same_url(self):
        router = Router()
        router.add("/v1.0/users/{user_id}", lambda request: None, name="user")

        self.assertEqual(router.url_for("user", user_id="café"), "/v1.0/users/caf%C3%A9")
