"""Regression coverage for opt-in browser security helpers."""
import unittest

from aurora.core.request import Request
from aurora.core.session import Session
from aurora.security import csrf_field, csrf_token, redirect_local, validate_csrf


def request():
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)
    req._session = Session()
    return req


class SecurityHelpersTests(unittest.TestCase):
    def test_local_redirect_keeps_query_and_defaults_to_303(self):
        response = redirect_local('/notes?page=2')
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['Location'], '/notes?page=2')

    def test_local_redirect_rejects_external_and_ambiguous_targets(self):
        for target in ('https://evil.example', '//evil.example', '/\\evil.example',
                       '/notes\r\nX-Test: injected', 'notes', '/notes#fragment',
                       '/\x00notes', '/%2fexample.com', '/%5cother', None):
            with self.subTest(target=target), self.assertRaises(ValueError):
                redirect_local(target)

    def test_csrf_field_renders_valid_token(self):
        req = request()
        field = str(csrf_field(req))
        token = csrf_token(req)
        self.assertIn('name="csrf_token"', field)
        self.assertIn(f'value="{token}"', field)
        self.assertTrue(validate_csrf(req, token))

    def test_csrf_field_is_safe_for_autoescaped_templates(self):
        from jinja2 import Environment
        req = request()
        html = Environment(autoescape=True).from_string('{{ field }}').render(field=csrf_field(req))
        self.assertTrue(html.startswith('<input '))
        self.assertNotIn('&lt;input', html)
