import asyncio
import unittest

from aurora.core.application import Application
from aurora.core.controller import Controller


async def request(app, path, method="GET"):
    sent = []
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(event):
        sent.append(event)
    await app({"type": "http", "path": path, "method": method}, receive, send)
    return sent[0]["status"], sent[-1].get("body", b"")


class LocaleController(Controller):
    def get(self):
        return "ok"


class MultilingualRoutingTests(unittest.TestCase):
    def test_legacy_app_unchanged(self):
        app = Application()
        app.add_route("/notes", lambda request: "legacy", name="notes")
        self.assertEqual(asyncio.run(request(app, "/notes")), (200, b"legacy"))
        self.assertEqual(app.url_for("notes"), "/notes")

    def test_always_prefix_and_locale_is_request_scoped(self):
        app = Application(locales=["en", "ku"], default_locale="en")
        app.add_route("/notes/{id}", lambda request, id: f"{request.locale}:{id}:{request.path}", name="note")
        self.assertEqual(asyncio.run(request(app, "/en/notes/7")), (200, b"en:7:/en/notes/7"))
        self.assertEqual(asyncio.run(request(app, "/ku/notes/8")), (200, b"ku:8:/ku/notes/8"))
        self.assertEqual(asyncio.run(request(app, "/notes/7"))[0], 404)
        self.assertEqual(asyncio.run(request(app, "/fr/notes/7"))[0], 404)

    def test_named_url_generation_and_param_collision(self):
        app = Application(locales=["en", "ku"], default_locale="en")
        app.add_route("/items/{locale}", lambda request, locale: locale, name="item")
        self.assertEqual(app.url_for("item", locale="x"), "/items/x")
        self.assertEqual(app.localized_url_for("item", language="ku", locale="x"), "/ku/items/x")
        with self.assertRaises(ValueError):
            app.localized_url_for("item", language="fr", locale="x")

    def test_controller_405_and_root(self):
        app = Application(locales=["en", "ku"], default_locale="en")
        app.route("/notes", LocaleController)
        app.add_route("/", lambda request: request.locale, name="home")
        self.assertEqual(asyncio.run(request(app, "/en/notes", "POST"))[0], 405)
        self.assertEqual(asyncio.run(request(app, "/notes", "POST"))[0], 404)
        self.assertEqual(asyncio.run(request(app, "/ku")), (200, b"ku"))
        self.assertEqual(app.localized_url_for("home", language="en"), "/en")

    def test_concurrent_locale_isolation(self):
        app = Application(locales=["en", "ku"], default_locale="en")
        async def handler(request):
            await asyncio.sleep(0)
            return request.locale
        app.add_route("/", handler)
        async def run():
            return await asyncio.gather(request(app, "/en"), request(app, "/ku"))
        self.assertEqual(asyncio.run(run()), [(200, b"en"), (200, b"ku")])

    def test_config_validation(self):
        for kw in ({"locales": ["en"]}, {"locales": ["en", "en"], "default_locale": "en"},
                   {"locales": ["../"], "default_locale": "../"}, {"default_locale": "en"}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                Application(**kw)

    def test_middleware_sees_original_path_and_locale(self):
        app = Application(locales=["en", "ku"], default_locale="en")
        @app.add_middleware
        async def locale_header(request, call_next):
            response = await call_next(request)
            response.headers["x-locale"] = request.locale or "none"
            return response
        app.add_route("/notes", lambda request: "yes")
        self.assertEqual(asyncio.run(request(app, "/ku/notes")), (200, b"yes"))


if __name__ == "__main__":
    unittest.main()
