import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora.core.application import Application
from aurora.view import View
from tests.fixtures.multilingual_views import app


async def fetch(application, path, cookie=None):
    events = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(event):
        events.append(event)
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    await application({"type": "http", "method": "GET", "path": path,
                       "headers": headers}, receive, send)
    return events[0]["status"], events[-1]["body"].decode("utf-8")


class MultilingualTemplateNavigationTests(unittest.TestCase):
    def test_current_language_link_and_switch_link(self):
        status, body = asyncio.run(fetch(app, "/en/"))
        self.assertEqual(status, 200)
        self.assertIn('href="/en/articles/42"', body)
        self.assertIn('href="/ku"', body)

    def test_cookie_does_not_override_url_in_template(self):
        status, body = asyncio.run(fetch(app, "/ar/articles/42", "preferred_language=en"))
        self.assertEqual(status, 200)
        self.assertIn('lang="ar"', body)
        self.assertIn('href="/ar"', body)

    def test_unprefixed_route_still_404(self):
        self.assertEqual(asyncio.run(fetch(app, "/articles/42"))[0], 404)

    def test_concurrent_requests_have_isolated_locales(self):
        async def both():
            return await asyncio.gather(fetch(app, "/en/"), fetch(app, "/ku/"))
        first, second = asyncio.run(both())
        self.assertIn('href="/en/articles/42"', first[1])
        self.assertIn('href="/ku/articles/42"', second[1])

    def test_existing_nonlocalized_views_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "page.html").write_text("<p>{{ title }}</p>")
            plain = Application(views_directory=directory)
            plain.add_route("/", lambda request: View("page", title="OK"))
            self.assertEqual(asyncio.run(fetch(plain, "/")), (200, "<p>OK</p>"))

    def test_existing_view_context_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "page.html").write_text("{{ site }} {{ locale }} {{ localized_url_for('home') }}")
            configured = Application(views_directory=directory, locales=["en", "ku"],
                                     default_locale="en", view_context=lambda req: {"site": "AIO"})
            configured.add_route("/", lambda req: View("page"), name="home")
            self.assertEqual(asyncio.run(fetch(configured, "/ku/")), (200, "AIO ku /ku"))


if __name__ == "__main__":
    unittest.main()
