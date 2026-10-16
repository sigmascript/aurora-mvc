import unittest

from aurora.core.response import RedirectResponse, Response


class ResponseHeaderHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def assert_rejected(self, response):
        messages = []

        async def send(message):
            messages.append(message)

        with self.assertRaises(ValueError):
            await response(send)
        self.assertEqual(messages, [])

    async def test_rejects_newline_in_header_value(self):
        await self.assert_rejected(Response(headers={"X-Test": "ok\r\nX-Evil: yes"}))

    async def test_rejects_invalid_header_name(self):
        await self.assert_rejected(Response(headers={"X-Test\r\nEvil": "yes"}))
        await self.assert_rejected(Response(headers={"Bad Header": "yes"}))

    async def test_rejects_newline_in_content_type(self):
        await self.assert_rejected(Response(content_type="text/plain\r\nX-Evil: yes"))

    async def test_rejects_newline_in_redirect_location(self):
        await self.assert_rejected(RedirectResponse("/safe\r\nX-Evil: yes"))

    async def test_valid_tabs_and_latin1_values(self):
        messages = []

        async def send(message):
            messages.append(message)

        await Response(headers={"X-Note": "caf\xe9\tvalue"})(send)
        self.assertIn([b"x-note", b"caf\xe9\tvalue"], messages[0]["headers"])

    async def test_rejects_control_characters(self):
        await self.assert_rejected(Response(headers={"X-Test": "a\x00b"}))
        await self.assert_rejected(Response(headers={"X-Test": "a\x7fb"}))
