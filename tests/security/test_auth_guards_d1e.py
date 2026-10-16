"""D1-E regression tests for return URL boundaries and request methods."""
import unittest
from aurora.core.application import Application
from aurora.core.controller import Controller
from aurora.core.request import Request
from aurora.core.http_exception import HTTPException
from aurora.security import login_required, safe_next, request_next
from aurora.urls import _current_application


def authenticated(request):
    return bool(request.scope.get('authenticated'))


class AuthSecurityCloseoutTests(unittest.TestCase):
    def setUp(self):
        app = Application()
        app.add_route('/signin', lambda request: 'signin', name='auth.signin')
        self.token = _current_application.set(app)

    def tearDown(self):
        _current_application.reset(self.token)

    def request(self, method='GET', path='/private', query=b''):
        return Request({'type': 'http', 'method': method, 'path': path,
                        'query_string': query})

    def test_head_redirect_includes_next_without_replay(self):
        class Private(Controller):
            @login_required('auth.signin', authenticated=authenticated, with_next=True)
            def head(self):
                return 'private'
        result = Private(self.request('HEAD')).head()
        self.assertEqual(result.status_code, 303)
        self.assertEqual(result.headers['Location'], '/signin?next=%2Fprivate')

    def test_mutating_methods_reject_unauthenticated_requests(self):
        class Private(Controller):
            @login_required('auth.signin', authenticated=authenticated, with_next=True)
            def post(self):
                return 'private'
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            with self.subTest(method=method), self.assertRaises(HTTPException) as ctx:
                Private(self.request(method)).post()
            self.assertEqual(ctx.exception.status_code, 403)

    def test_nested_encoding_and_control_characters_are_rejected(self):
        for path in ('/%25252f%25252fevil.test', '/%250aevil', '/%5c%5cevil',
                     '/private\x00bad', '/private\r\nLocation:evil'):
            with self.subTest(path=path):
                self.assertEqual(safe_next(path, '/safe'), '/safe')

    def test_missing_or_ambiguous_next_uses_fallback(self):
        for query in (b'', b'next=', b'next=%2Fone&next=%2Ftwo',
                      b'next=https%3A%2F%2Fevil.test'):
            with self.subTest(query=query):
                self.assertEqual(request_next(self.request(query=query), '/safe'), '/safe')


if __name__ == '__main__':
    unittest.main()
