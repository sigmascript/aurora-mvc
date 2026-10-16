import unittest

from aurora.core.request import Request
from aurora.core.router import Router


class TestRouter(unittest.TestCase):

    def test_resolve_route(self):
        router = Router()

        def handler(request):
            return "Hello"

        router.add(
            "/hello",
            handler,
            methods=["GET"],
        )

        request = Request({
            "method": "GET",
            "path": "/hello",
        })

        route, params = router.resolve(request)

        self.assertIsNotNone(route)
        self.assertEqual(route.handler, handler)
        self.assertEqual(params, {})

    def test_route_not_found(self):
        router = Router()

        request = Request({
            "method": "GET",
            "path": "/missing",
        })

        route, params = router.resolve(request)

        self.assertIsNone(route)
        self.assertEqual(params, {})

    def test_resolve_dynamic_route(self):
        router = Router()

        def handler(request, id):
            return id

        router.add(
            "/users/{id}",
            handler,
            methods=["GET"],
        )

        request = Request({
            "method": "GET",
            "path": "/users/123",
        })

        route, params = router.resolve(request)

        self.assertIsNotNone(route)
        self.assertEqual(route.handler, handler)
        self.assertEqual(params, {"id": "123"})


if __name__ == "__main__":
    unittest.main()
