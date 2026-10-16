"""B3 deployment and CSRF boundary contract tests."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aurora.core.http_exception import HTTPException
from aurora.core.response import Response
from aurora.security.csrf import csrf_token, validate_csrf
from aurora.security.sessions import SessionSettings
from aurora.core.session import Session


class DeploymentSecurityAuditTests(unittest.TestCase):
    def test_default_session_cookie_requires_https(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = SessionSettings(database_path=str(Path(directory) / 'sessions.sqlite3'))
            self.assertTrue(settings.secure)
            self.assertEqual(settings.samesite, 'Lax')

    def test_host_cookie_is_secure_http_only_and_root_scoped(self):
        response = Response('ok')
        response.set_cookie('__Host-aurora_session', 'token', secure=True)
        cookie = response._cookie_headers[('__Host-aurora_session', '/')].decode('ascii')
        self.assertIn('Secure', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Path=/', cookie)
        self.assertNotIn('Domain=', cookie)

    def test_csrf_rejects_missing_token_without_creating_session_token(self):
        request = SimpleNamespace(method='POST', session=Session())
        with self.assertRaises(HTTPException) as error:
            validate_csrf(request, None)
        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(dict(request.session), {})

    def test_csrf_rejects_valid_token_for_safe_method(self):
        request = SimpleNamespace(method='GET', session=Session())
        token = csrf_token(request)
        with self.assertRaises(HTTPException) as error:
            validate_csrf(request, token)
        self.assertEqual(error.exception.status_code, 405)


if __name__ == '__main__':
    unittest.main()
