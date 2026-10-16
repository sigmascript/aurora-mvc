import asyncio
import unittest

from tests.fixtures.multilingual_navigation import app


async def fetch(path, *, cookie=None):
    events = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(event):
        events.append(event)
    headers = [(b"cookie", cookie.encode("ascii"))] if cookie else []
    await app({"type": "http", "method": "GET", "path": path, "headers": headers}, receive, send)
    return events


class MultilingualNavigationTests(unittest.TestCase):
    def test_article_uses_url_language_without_session(self):
        events = asyncio.run(fetch("/ku/articles/42"))
        self.assertEqual(events[0]["status"], 200)
        self.assertEqual(events[-1]["body"], b"Article 42; language: ku")
        self.assertIn([b"content-language", b"ku"], events[0]["headers"])

    def test_switch_article_redirects_to_equivalent_named_route(self):
        events = asyncio.run(fetch("/en/switch/article/ku/42"))
        self.assertEqual(events[0]["status"], 303)
        self.assertIn([b"location", b"/ku/articles/42"], events[0]["headers"])
        self.assertTrue(any(k == b"set-cookie" and b"preferred_language=ku" in v
                            for k, v in events[0]["headers"]))

    def test_cookie_never_overrides_explicit_url(self):
        events = asyncio.run(fetch("/ar/articles/42", cookie="preferred_language=en"))
        self.assertEqual(events[-1]["body"], b"Article 42; language: ar")

    def test_invalid_language_cannot_redirect_or_set_cookie(self):
        events = asyncio.run(fetch("/en/switch/article/fr/42"))
        self.assertEqual(events[0]["status"], 404)
        self.assertFalse(any(k == b"set-cookie" for k, _ in events[0]["headers"]))

    def test_unprefixed_route_is_not_accessible(self):
        self.assertEqual(asyncio.run(fetch("/articles/42"))[0]["status"], 404)

    def test_named_url_escapes_route_parameter(self):
        self.assertEqual(app.localized_url_for("article", language="ku", id="a b"),
                         "/ku/articles/a%20b")


if __name__ == "__main__":
    unittest.main()
