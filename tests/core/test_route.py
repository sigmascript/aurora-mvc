import unittest

from aurora.core.request import Request
from aurora.core.route import Route


class TestRoute(unittest.TestCase):

    def test_route_matches_request(self):
        def handler(request):
            return "Hello"

        route = Route(
            "/hello",
            handler,
            methods=["GET"],
        )

        request = Request({
            "method": "GET",
            "path": "/hello",
        })

        params = route.matches(request)

        self.assertIsNotNone(params)
        self.assertEqual(params, {})

    def test_route_does_not_match_wrong_method(self):
        def handler(request):
            return "Hello"

        route = Route(
            "/hello",
            handler,
            methods=["GET"],
        )

        request = Request({
            "method": "POST",
            "path": "/hello",
        })

        self.assertIsNone(route.matches(request))

    def test_dynamic_route_parameters(self):
        def handler(request, id):
            return id

        route = Route(
            "/users/{id}",
            handler,
            methods=["GET"],
        )

        request = Request({
            "method": "GET",
            "path": "/users/42",
        })

        params = route.matches(request)

        self.assertEqual(params, {"id": "42"})


    def test_dynamic_route_does_not_cross_slashes(self):
        def handler(request, id):
            return id

        route = Route(
            "/users/{id}",
            handler,
            methods=["GET"],
        )

        request = Request({
            "method": "GET",
            "path": "/users/42/profile",
        })

        self.assertIsNone(route.matches(request))


if __name__ == "__main__":
    unittest.main()
