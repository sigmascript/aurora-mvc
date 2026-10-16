import asyncio
import unittest

from aurora import Aurora, Controller, View, url_for
from aurora.core.router import Router


class Blog(Controller):
    def get(self):
        return 'Blog'


class TestNamedRoutes(unittest.TestCase):
    def test_named_controller_and_function_routes(self):
        app = Aurora()
        app.route('/blog', Blog, name='blog')
        app.add_route('/posts/{id}', lambda request, id: id, name='post.detail')
        self.assertEqual(app.url_for('blog'), '/blog')
        self.assertEqual(app.url_for('post.detail', id=42), '/posts/42')

    def test_duplicate_name_does_not_register_route(self):
        router = Router()
        router.add('/a', lambda request: '', name='home')
        with self.assertRaises(ValueError):
            router.add('/b', lambda request: '', name='home')
        self.assertEqual(len(router.routes), 1)

    def test_missing_extra_unknown_parameters(self):
        app = Aurora()
        app.add_route('/items/{id}', lambda request, id: id, name='item')
        with self.assertRaises(ValueError):
            app.url_for('item')
        with self.assertRaises(ValueError):
            app.url_for('item', id=1, unused=2)
        with self.assertRaises(LookupError):
            app.url_for('missing')

    def test_encoded_segment(self):
        app = Aurora()
        app.add_route('/search/{term}', lambda request, term: term, name='search')
        self.assertEqual(app.url_for('search', term='café & tea'), '/search/caf%C3%A9%20%26%20tea')
        for value in ('../secret', '.', '', 'a/b', 'a\\b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                app.url_for('search', term=value)

    def test_request_local_helper(self):
        app = Aurora()
        app.add_route('/item/{id}', lambda request, id: url_for('item', id=id), name='item')
        async def call():
            events = []
            async def receive():
                return {'type': 'http.request', 'body': b'', 'more_body': False}
            async def send(event):
                events.append(event)
            await app({'type': 'http', 'path': '/item/12', 'method': 'GET'}, receive, send)
            return events
        events = asyncio.run(call())
        self.assertEqual(events[-1]['body'], b'/item/12')
        with self.assertRaises(RuntimeError):
            url_for('item', id=12)

    def test_jinja_global(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            Path(tmp, 'page.html').write_text('{{ url_for("article", id=7) }}', encoding='utf-8')
            app = Aurora(views_directory=tmp)
            app.add_route('/article/{id}', lambda request, id: id, name='article')
            self.assertEqual(app.renderer.render(View('page')), '/article/7')

    def test_existing_unnamed_route(self):
        app = Aurora()
        app.route('/blog', Blog)
        self.assertIsNone(app.router.routes[0].name)


if __name__ == '__main__':
    unittest.main()
