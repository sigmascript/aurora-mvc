import asyncio
import unittest
from aurora.core.application import Application
from aurora.core.response import Response


async def run(app, path, cookie=None):
    events = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(event):
        events.append(event)
    headers = [(b"cookie", cookie.encode("latin-1"))] if cookie else []
    await app({"type": "http", "path": path, "method": "GET", "headers": headers}, receive, send)
    return events


class LocalePreferenceTests(unittest.TestCase):
    def make_app(self):
        return Application(locales=["en", "ku"], default_locale="en", locale_cookie_name="preferred_language")

    def test_cookie_is_optional_without_sessions(self):
        app = self.make_app()
        app.add_route("/", lambda request: f"{request.locale}:{request.preferred_locale}")
        self.assertEqual(asyncio.run(run(app, "/ku"))[-1]["body"], b"ku:None")

    def test_url_locale_wins_over_preference(self):
        app = self.make_app()
        app.add_route("/notes", lambda request: f"{request.locale}:{request.preferred_locale}")
        self.assertEqual(asyncio.run(run(app, "/ku/notes", "preferred_language=en"))[-1]["body"], b"ku:en")

    def test_invalid_cookie_ignored(self):
        app = self.make_app()
        app.add_route("/", lambda request: str(request.preferred_locale))
        self.assertEqual(asyncio.run(run(app, "/en", "preferred_language=fr"))[-1]["body"], b"None")

    def test_explicit_cookie_write_and_security_attributes(self):
        app = self.make_app()
        def handler(request):
            return app.remember_locale(Response("ok"), request.locale, secure=True)
        app.add_route("/", handler)
        headers = asyncio.run(run(app, "/ku"))[0]["headers"]
        cookie = next(v for k,v in headers if k == b"set-cookie")
        self.assertIn(b"preferred_language=ku", cookie)
        self.assertIn(b"Secure", cookie)
        self.assertIn(b"HttpOnly", cookie)
        self.assertIn(b"SameSite=Lax", cookie)

    def test_no_automatic_cookie_write(self):
        app = self.make_app()
        app.add_route("/", lambda request: "ok")
        self.assertFalse(any(k == b"set-cookie" for k,v in asyncio.run(run(app, "/ku"))[0]["headers"]))

    def test_validation(self):
        with self.assertRaises(ValueError):
            Application(locale_cookie_name="preferred_language")
        with self.assertRaises(ValueError):
            Application(locales=["en"], default_locale="en", locale_cookie_name="bad;name")
        app = self.make_app()
        with self.assertRaises(ValueError):
            app.remember_locale(Response(), "fr")


if __name__ == "__main__":
    unittest.main()
