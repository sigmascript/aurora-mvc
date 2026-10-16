"""Black-box ASGI session lifecycle tests using real response headers."""
import tempfile
import unittest
from pathlib import Path
from http.cookies import SimpleCookie

from aurora import Application
from aurora.security.sessions import SessionSettings
from aurora.core.response import RedirectResponse


class TestSessionLifecycleIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Application(sessions=True, session_settings=SessionSettings(
            database_path=str(Path(self.temp.name) / 'sessions.sqlite3'), secure=False,
        ))

        async def login(request):
            await request.session.regenerate()
            request.session['user_id'] = 7
            return RedirectResponse('/profile', status_code=303)

        async def profile(request):
            return str(request.session.get('user_id', 'anonymous'))

        async def logout(request):
            await request.session.invalidate()
            return RedirectResponse('/profile', status_code=303)

        async def modify(request):
            request.session['theme'] = 'dark'
            return 'saved'

        self.app.add_route('/login', login, methods=['POST'])
        self.app.add_route('/profile', profile)
        self.app.add_route('/logout', logout, methods=['POST'])
        self.app.add_route('/modify', modify, methods=['POST'])

    async def request(self, path, *, method='GET', cookie=None):
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        headers = [(b'cookie', f'aurora_session={cookie}'.encode())] if cookie else []
        await self.app({'type': 'http', 'path': path, 'method': method,
                        'headers': headers}, receive, send)
        start = messages[0]
        body = messages[1]['body'].decode()
        cookies = [v.decode() for k, v in start['headers'] if k == b'set-cookie']
        return start['status'], body, cookies

    @staticmethod
    def token(cookie_header):
        jar = SimpleCookie()
        jar.load(cookie_header)
        return jar['aurora_session'].value

    async def test_anonymous_read_does_not_create_cookie(self):
        status, body, cookies = await self.request('/profile')
        self.assertEqual((status, body, cookies), (200, 'anonymous', []))

    async def test_login_persists_and_logout_revokes(self):
        status, _, cookies = await self.request('/login', method='POST')
        self.assertEqual(status, 303)
        self.assertEqual(len(cookies), 1)
        self.assertIn('httponly', cookies[0].lower())
        self.assertIn('samesite=lax', cookies[0].lower())
        token = self.token(cookies[0])
        self.assertEqual((await self.request('/profile', cookie=token))[1], '7')
        status, _, cookies = await self.request('/logout', method='POST', cookie=token)
        self.assertEqual(status, 303)
        self.assertIn('max-age=0', cookies[0].lower())
        self.assertEqual((await self.request('/profile', cookie=token))[1], 'anonymous')

    async def test_unknown_token_is_cleared(self):
        _, body, cookies = await self.request('/profile', cookie='unknown')
        self.assertEqual(body, 'anonymous')
        self.assertEqual(len(cookies), 1)
        self.assertIn('max-age=0', cookies[0].lower())

    async def test_mutation_creates_session_only_when_needed(self):
        _, _, cookies = await self.request('/modify', method='POST')
        self.assertEqual(len(cookies), 1)
        token = self.token(cookies[0])
        self.assertEqual((await self.request('/profile', cookie=token))[1], 'anonymous')

    async def test_opt_out_does_not_enable_sessions(self):
        plain = Application()
        def handler(request):
            try:
                request.session
            except RuntimeError:
                return 'not configured'
            return 'unexpected'
        plain.add_route('/', handler)
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(msg):
            messages.append(msg)
        await plain({'type': 'http', 'path': '/', 'method': 'GET'}, receive, send)
        self.assertEqual(messages[1]['body'], b'not configured')
