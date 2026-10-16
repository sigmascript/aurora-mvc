"""Checkpoint C5: multilingual routing compatibility and integration boundaries."""
import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.response import Response


async def get(app, path, cookie=None):
    events = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(event):
        events.append(event)
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    await app({"type": "http", "method": "GET", "path": path,
               "headers": headers}, receive, send)
    return events[0]["status"], events[-1]["body"].decode("utf-8")


class MultilingualCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.app = Application(locales=["en", "ku", "ar"], default_locale="en",
                               locale_cookie_name="preferred_language")
        self.app.add_route("/articles/{slug}",
                           lambda req, slug: Response(f"{req.locale}:{req.preferred_locale}:{slug}"),
                           name="article")

    def test_all_languages_require_prefix_and_unknown_rejected(self):
        for path in ("/articles/hello", "/fr/articles/hello"):
            with self.subTest(path=path):
                self.assertEqual(asyncio.run(get(self.app, path))[0], 404)
        for language in ("en", "ku", "ar"):
            self.assertEqual(asyncio.run(get(self.app, f"/{language}/articles/hello"))[0], 200)

    def test_cookie_never_overrides_url_locale(self):
        self.assertEqual(asyncio.run(get(self.app, "/ku/articles/hello", "preferred_language=en")),
                         (200, "ku:en:hello"))
        self.assertEqual(asyncio.run(get(self.app, "/ar/articles/hello", "preferred_language=fr")),
                         (200, "ar:None:hello"))

    def test_named_route_escapes_translated_slug_as_segment(self):
        url = self.app.localized_url_for("article", language="ku", slug="هەواڵی نوێ")
        self.assertTrue(url.startswith("/ku/articles/"))
        self.assertIn("%", url)
        for slug in ("../admin", "one/two", ".."):
            with self.subTest(slug=slug):
                with self.assertRaises(ValueError):
                    self.app.localized_url_for("article", language="en", slug=slug)

    def test_switching_translated_slug_is_application_responsibility(self):
        # AIO maps a stable content ID to its translated slug; the router only builds URLs.
        translations = {"en": "latest-news", "ku": "هەواڵی-نوێ"}
        en = self.app.localized_url_for("article", language="en", slug=translations["en"])
        ku = self.app.localized_url_for("article", language="ku", slug=translations["ku"])
        self.assertNotEqual(en, ku)
        self.assertEqual(en, "/en/articles/latest-news")
        self.assertTrue(ku.startswith("/ku/articles/"))

    def test_nonlocalized_app_keeps_existing_route_contract(self):
        plain = Application()
        plain.add_route("/articles/{slug}", lambda req, slug: Response(slug), name="article")
        self.assertEqual(asyncio.run(get(plain, "/articles/hello")), (200, "hello"))
        self.assertEqual(plain.url_for("article", slug="hello"), "/articles/hello")
        with self.assertRaises(RuntimeError):
            plain.localized_url_for("article", language="en", slug="hello")


if __name__ == "__main__":
    unittest.main()
