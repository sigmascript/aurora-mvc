import asyncio
import tempfile
import unittest
from pathlib import Path

from aurora import Application
from aurora.security.sessions import SessionSettings, SQLiteSessionStore, SessionConflict


async def request(app, path='/', cookie=None):
    sent = []
    headers = [(b'cookie', cookie.encode())] if cookie else []
    scope = {'type': 'http', 'method': 'GET', 'path': path, 'headers': headers}
    async def receive():
        return {'type': 'http.request', 'body': b'', 'more_body': False}
    async def send(message):
        sent.append(message)
    await app(scope, receive, send)
    start = sent[0]
    cookies = [v.decode() for k, v in start['headers'] if k == b'set-cookie']
    return start['status'], cookies, sent[-1]['body']


class TestSessionStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = SessionSettings(str(Path(self.tmp.name) / 'sessions.db'), secure=False)

    def app(self):
        return Application(sessions=True, session_settings=self.settings)

    async def test_opt_in_and_no_cookie_on_read_only_request(self):
        app = self.app()
        app.add_route('/', lambda req: str(req.session.get('name', 'guest')))
        status, cookies, body = await request(app)
        self.assertEqual((status, cookies, body), (200, [], b'guest'))
        self.assertIsNone(Application().session_manager)

    async def test_roundtrip_cookie_and_persistence(self):
        app = self.app()
        def set_name(req):
            req.session['name'] = 'Aurora'
            return 'ok'
        app.add_route('/set', set_name)
        app.add_route('/get', lambda req: req.session.get('name', 'missing'))
        status, cookies, _ = await request(app, '/set')
        self.assertEqual(status, 200)
        self.assertEqual(len(cookies), 1)
        self.assertIn('httponly', cookies[0].lower())
        cookie = cookies[0].split(';')[0]
        status, cookies, body = await request(app, '/get', cookie)
        self.assertEqual((status, cookies, body), (200, [], b'Aurora'))

    async def test_regeneration_revokes_old_identifier(self):
        app = self.app()
        app.add_route('/set', lambda req: req.session.__setitem__('user', 5) or 'ok')
        async def rotate(req):
            await req.session.regenerate()
            return 'rotated'
        app.add_route('/rotate', rotate)
        app.add_route('/get', lambda req: str(req.session.get('user', 'none')))
        _, cookies, _ = await request(app, '/set')
        old = cookies[0].split(';')[0]
        _, cookies, _ = await request(app, '/rotate', old)
        new = cookies[0].split(';')[0]
        self.assertNotEqual(old, new)
        self.assertEqual((await request(app, '/get', old))[2], b'none')
        self.assertEqual((await request(app, '/get', new))[2], b'5')

    async def test_logout_revokes_and_clears_cookie(self):
        app = self.app()
        app.add_route('/set', lambda req: req.session.__setitem__('user', 5) or 'ok')
        async def logout(req):
            await req.session.invalidate()
            return 'bye'
        app.add_route('/logout', logout)
        app.add_route('/get', lambda req: str(req.session.get('user', 'none')))
        _, cookies, _ = await request(app, '/set')
        old = cookies[0].split(';')[0]
        status, cookies, _ = await request(app, '/logout', old)
        self.assertEqual(status, 200)
        self.assertIn('Max-Age=0', cookies[0])
        self.assertEqual((await request(app, '/get', old))[2], b'none')

    async def test_stale_writer_cannot_resurrect_revoked_session(self):
        store = SQLiteSessionStore(self.settings.database_path)
        token = store.commit(None, None, {'user': 1})
        data, version = store.load(token)
        store.commit(token, version, {}, invalidate=True)
        with self.assertRaises(SessionConflict):
            store.commit(token, version, {'user': 1})
        self.assertIsNone(store.load(token))

    async def test_stale_writer_cannot_overwrite_newer_data(self):
        store = SQLiteSessionStore(self.settings.database_path)
        token = store.commit(None, None, {'n': 1})
        _, version = store.load(token)
        store.commit(token, version, {'n': 2})
        with self.assertRaises(SessionConflict):
            store.commit(token, version, {'n': 3})
        self.assertEqual(store.load(token)[0]['n'], 2)

    async def test_storage_error_fails_closed(self):
        app = self.app()
        app.add_route('/', lambda req: req.session.__setitem__('user', 1) or 'ok')
        app.session_manager.store.path = '/nonexistent-aurora-directory/session.db'
        status, cookies, body = await request(app)
        self.assertEqual(status, 503)
        self.assertFalse(cookies)

    async def test_opt_in_requires_configuration(self):
        with self.assertRaises(ValueError):
            Application(sessions=True)
        with self.assertRaises(ValueError):
            Application(session_settings=self.settings)
