import unittest

from aurora.view import View, view


class TestView(unittest.TestCase):

    def test_view_stores_template(self):
        page = View(
            "home.html",
        )

        self.assertEqual(
            page.name,
            "home.html",
        )

    def test_view_stores_context(self):
        page = View(
            "users/profile.html",
            {
                "name": "Aurora",
                "version": 1,
            },
        )

        self.assertEqual(
            page.context,
            {
                "name": "Aurora",
                "version": 1,
            },
        )

    def test_view_has_empty_context_by_default(self):
        page = View(
            "home.html",
        )

        self.assertEqual(
            page.context,
            {},
        )

    def test_view_copies_context(self):
        context = {
            "title": "Aurora",
        }

        page = View(
            "home.html",
            context,
        )

        context["title"] = "Changed"

        self.assertEqual(
            page.context["title"],
            "Aurora",
        )

    def test_view_helper(self):
        page = view(
            "users/profile.html",
            name="Aurora",
            version=1,
        )

        self.assertIsInstance(
            page,
            View,
        )

        self.assertEqual(
            page.name,
            "users/profile.html",
        )

        self.assertEqual(
            page.context,
            {
                "name": "Aurora",
                "version": 1,
            },
        )


if __name__ == "__main__":
    unittest.main()
