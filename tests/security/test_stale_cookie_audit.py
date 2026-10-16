"""B3 regression tests: expired/unknown cookies and session cookie ownership."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora import Application
from aurora.core.response import Response
from aurora.security.sessions import SessionSettings


class StaleCookieAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(sessions=SessionSettings(
            database_path=str(Path(self.temp.name) / 'sessions.sqlite3'), secure=False,
        ))
        self.app.add_route('/read', lambda request: str(request.session.get('user', 'anonymous')))
        self.app.add_route('/write', self.write, methods=['POST'])
        self.app.add_route('/forge', self.forge)

    @staticmethod
    def write(request):
        request.session['user'] = 'verified'
        return 'saved'

    @staticmethod
    def forge(request):
        response = Response('forged')
        response.set_cookie('aurora_session', 'forged')
        return response

    async def call(self, path, *, method='GET', cookie=None):
        sent = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            sent.append(message)
        headers = [(b'cookie', ('aurora_session=' + cookie).encode('ascii'))] if cookie else []
        await self.app({'type': 'http', 'method': method, 'path': path, 'headers': headers}, receive, send)
        return sent[0]['status'], sent[1]['body'], [v.decode('ascii') for k, v in sent[0]['headers'] if k == b'set-cookie']

    async def test_unknown_cookie_is_expired_without_new_session(self):
        status, body, cookies = await self.call('/read', cookie='nonexistent')
        self.assertEqual((status, body), (200, b'anonymous'))
        self.assertEqual(len(cookies), 1)
        self.assertIn('max-age=0', cookies[0].lower())
        self.assertIn('httponly', cookies[0].lower())

    async def test_expired_cookie_cannot_restore_authentication(self):
        with patch('aurora.security.sessions.time.time', return_value=100):
            token = self.app.session_manager.store.commit(None, None, {'user': 'verified'}, lifetime=10)
        with patch('aurora.security.sessions.time.time', return_value=111):
            status, body, cookies = await self.call('/read', cookie=token)
        self.assertEqual((status, body), (200, b'anonymous'))
        self.assertEqual(len(cookies), 1)
        self.assertIn('max-age=0', cookies[0].lower())

    async def test_valid_cookie_is_not_reissued_for_read_only_request(self):
        status, _, cookies = await self.call('/write', method='POST')
        self.assertEqual(status, 200)
        self.assertEqual(len(cookies), 1)
        from http.cookies import SimpleCookie
        jar = SimpleCookie()
        jar.load(cookies[0])
        token = jar['aurora_session'].value
        status, body, cookies = await self.call('/read', cookie=token)
        self.assertEqual((status, body, cookies), (200, b'verified', []))

    async def test_handler_cannot_forge_reserved_session_cookie(self):
        status, body, cookies = await self.call('/forge')
        self.assertEqual(status, 503)
        self.assertEqual(cookies, [])
        self.assertNotIn(b'forged', body)
