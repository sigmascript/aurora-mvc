"""Security regression tests for session cookie ownership and storage safety."""
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from aurora import Application
from aurora.core.response import Response
from aurora.security.sessions import SessionSettings


class TestSessionHardening(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / 'sessions.db')
        self.app = Application(sessions=True, session_settings=SessionSettings(database_path=self.path, secure=False, max_data_bytes=64))

    async def call(self, path, cookie=None):
        events = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(event):
            events.append(event)
        headers = [(b'cookie', ('aurora_session=' + cookie).encode())] if cookie else []
        await self.app({'type': 'http', 'path': path, 'method': 'GET', 'headers': headers}, receive, send)
        return events[0]['status'], events[0]['headers']

    async def test_reserved_cookie_cannot_be_set_by_handler(self):
        def handler(request):
            response = Response('ok')
            response.set_cookie('aurora_session', 'forged')
            return response
        self.app.add_route('/test', handler)
        status, headers = await self.call('/test')
        self.assertEqual(status, 503)
        self.assertFalse(any(k == b'set-cookie' for k, _ in headers))

    async def test_raw_set_cookie_header_rejected(self):
        self.app.add_route('/test', lambda request: Response('ok', headers={'Set-Cookie': 'aurora_session=forged'}))
        self.assertEqual((await self.call('/test'))[0], 503)

    async def test_session_size_limit_fails_closed(self):
        def handler(request):
            request.session['payload'] = 'x' * 100
            return 'ok'
        self.app.add_route('/test', handler)
        status, headers = await self.call('/test')
        self.assertEqual(status, 503)
        self.assertFalse(any(k == b'set-cookie' for k, _ in headers))

    async def test_corrupt_stored_session_fails_closed(self):
        token = 'testtoken'
        store = self.app.session_manager.store
        # sqlite3's context manager commits but does not close the connection.
        # Explicit closing is required so Windows can remove the temporary DB.
        with closing(sqlite3.connect(self.path)) as db:
            with db:
                db.execute(
                    'INSERT INTO aurora_sessions VALUES (?, ?, ?, ?)',
                    (store._hash(token), '{bad json', 99999999999, 1),
                )
        self.app.add_route('/test', lambda request: 'ok')
        self.assertEqual((await self.call('/test', cookie=token))[0], 503)

    async def test_valid_small_session_still_works(self):
        def handler(request):
            request.session['theme'] = 'dark'
            return 'ok'
        self.app.add_route('/test', handler)
        status, headers = await self.call('/test')
        self.assertEqual(status, 200)
        self.assertTrue(any(k == b'set-cookie' for k, _ in headers))

    def test_invalid_settings_rejected(self):
        with self.assertRaises(ValueError):
            SessionSettings(database_path=self.path, secure=False, cookie_name='__Host-session')
        with self.assertRaises(ValueError):
            SessionSettings(database_path=self.path, max_data_bytes=0)
