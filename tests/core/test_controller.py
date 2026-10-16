import unittest

from aurora.core.controller import Controller
from aurora.core.request import Request


class TestController(unittest.TestCase):

    def test_controller_has_request(self):
        request = Request({
            "method": "GET",
            "path": "/hello",
        })

        controller = Controller(request)

        self.assertIs(controller.request, request)

    def test_controller_action(self):
        class UserController(Controller):

            def show(self, id):
                return f"User: {id}"

        request = Request({
            "method": "GET",
            "path": "/users/42",
        })

        handler = UserController.action("show")

        result = handler(
            request,
            id="42",
        )

        self.assertEqual(result, "User: 42")

    def test_controller_action_has_request(self):
        class UserController(Controller):

            def show(self):
                return self.request.path

        request = Request({
            "method": "GET",
            "path": "/users",
        })

        handler = UserController.action("show")

        result = handler(request)

        self.assertEqual(result, "/users")


if __name__ == "__main__":
    unittest.main()
