import tempfile
import unittest
from pathlib import Path

from aurora.rendering import ViewRenderer
from aurora.view import view


class TestViewRenderer(unittest.TestCase):

    def test_render_view(self):
        with tempfile.TemporaryDirectory() as directory:
            template_path = (
                Path(directory) / "hello.html"
            )

            template_path.write_text(
                "<h1>Hello {{ name }}</h1>",
                encoding="utf-8",
            )

            renderer = ViewRenderer(
                directory
            )

            page = view(
                "hello.html",
                name="Aurora",
            )

            html = renderer.render(page)

            self.assertEqual(
                html,
                "<h1>Hello Aurora</h1>",
            )

    def test_render_view_with_condition(self):
        with tempfile.TemporaryDirectory() as directory:
            template_path = (
                Path(directory) / "account.html"
            )

            template_path.write_text(
                (
                    "{% if logged_in %}"
                    "<p>Welcome</p>"
                    "{% else %}"
                    "<p>Please log in</p>"
                    "{% endif %}"
                ),
                encoding="utf-8",
            )

            renderer = ViewRenderer(
                directory
            )

            page = view(
                "account.html",
                logged_in=True,
            )

            html = renderer.render(page)

            self.assertEqual(
                html,
                "<p>Welcome</p>",
            )

    def test_html_is_autoescaped(self):
        with tempfile.TemporaryDirectory() as directory:
            template_path = (
                Path(directory) / "message.html"
            )

            template_path.write_text(
                "<p>{{ message }}</p>",
                encoding="utf-8",
            )

            renderer = ViewRenderer(
                directory
            )

            page = view(
                "message.html",
                message="<script>alert('bad')</script>",
            )

            html = renderer.render(page)

            self.assertNotIn(
                "<script>",
                html,
            )

            self.assertIn(
                "&lt;script&gt;",
                html,
            )

    def test_renderer_requires_view(self):
        with tempfile.TemporaryDirectory() as directory:
            renderer = ViewRenderer(
                directory
            )

            with self.assertRaises(TypeError):
                renderer.render(
                    "home.html"
                )


if __name__ == "__main__":
    unittest.main()
