import unittest
from http.cookies import CookieError, SimpleCookie

from aurora.core.request import Request
from aurora.core.response import JSONResponse, RedirectResponse, Response


class TestCookies(unittest.IsolatedAsyncioTestCase):
    async def headers_for(self, response):
        messages = []

        async def send(message):
            messages.append(message)

        await response(send)
        return messages[0]["headers"]

    async def cookie_fields(self, response):
        headers = await self.headers_for(response)
        return [value.decode("ascii") for name, value in headers
                if name == b"set-cookie"]

    async def test_request_reads_values_and_quoted_values(self):
        request = Request({"headers": [(b"cookie", b'theme=dark; label="hello world"')]})
        self.assertEqual(request.cookies, {"theme": "dark", "label": "hello world"})

    async def test_missing_and_malformed_request_cookies(self):
        for headers in [[], [(b"cookie", b"not a valid cookie")],
                        [(b"cookie", b"bad@name=value")]]:
            with self.subTest(headers=headers):
                self.assertEqual(Request({"headers": headers}).cookies, {})

    async def test_repeated_cookie_headers_and_names(self):
        request = Request({"headers": [
            (b"cookie", b"theme=light"),
            (b"cookie", b"theme=dark; language=en"),
        ]})
        self.assertEqual(request.cookies, {"theme": "dark", "language": "en"})

    async def test_cookie_defaults(self):
        response = Response("Hello")
        response.set_cookie("theme", "dark")
        cookie = SimpleCookie()
        cookie.load((await self.cookie_fields(response))[0])
        self.assertEqual(cookie["theme"].value, "dark")
        self.assertEqual(cookie["theme"]["path"], "/")
        self.assertTrue(cookie["theme"]["httponly"])
        self.assertEqual(cookie["theme"]["samesite"], "Lax")
        self.assertFalse(cookie["theme"]["secure"])
        self.assertEqual(cookie["theme"]["max-age"], "")

    async def test_explicit_cookie_options(self):
        response = Response()
        response.set_cookie("token", "abc", path="/auth", max_age=600,
                            secure=True, httponly=False, samesite="strict")
        cookie = SimpleCookie()
        cookie.load((await self.cookie_fields(response))[0])
        self.assertEqual(cookie["token"]["path"], "/auth")
        self.assertEqual(cookie["token"]["max-age"], "600")
        self.assertTrue(cookie["token"]["secure"])
        self.assertFalse(cookie["token"]["httponly"])
        self.assertEqual(cookie["token"]["samesite"], "Strict")

    async def test_multiple_cookies_keep_separate_headers(self):
        response = Response()
        response.set_cookie("one", "1")
        response.set_cookie("two", "2")
        fields = await self.cookie_fields(response)
        self.assertEqual(len(fields), 2)
        self.assertTrue(fields[0].startswith("one=1"))
        self.assertTrue(fields[1].startswith("two=2"))

    async def test_same_scope_replaces_and_different_paths_coexist(self):
        response = Response()
        response.set_cookie("theme", "light")
        response.set_cookie("theme", "dark")
        response.set_cookie("theme", "special", path="/auth")
        fields = await self.cookie_fields(response)
        self.assertEqual(len(fields), 2)
        self.assertTrue(fields[0].startswith("theme=dark"))
        self.assertTrue(fields[1].startswith("theme=special"))

    async def test_delete_cookie_expires_matching_path(self):
        response = Response()
        response.set_cookie("token", "abc", path="/auth")
        response.delete_cookie("token", path="/auth")
        fields = await self.cookie_fields(response)
        self.assertEqual(len(fields), 1)
        self.assertIn("Max-Age=0", fields[0])
        self.assertIn("expires=Thu, 01 Jan 1970", fields[0])
        self.assertIn("Path=/auth", fields[0])

    async def test_cookie_value_round_trip_and_no_header_injection(self):
        for value in ['hello world; "quoted"', "a=b, c"]:
            with self.subTest(value=value):
                response = Response()
                response.set_cookie("label", value)
                field = (await self.cookie_fields(response))[0]
                self.assertNotIn("\r", field)
                self.assertNotIn("\n", field)
                pair = field.split("; Path=", 1)[0]
                request = Request({"headers": [(b"cookie", pair.encode("ascii"))]})
                self.assertEqual(request.cookies["label"], value)

    async def test_invalid_cookie_options_are_rejected(self):
        for options in [dict(path="/; injected=true"), dict(path="/\r\n"),
                        dict(max_age=True), dict(max_age="60"),
                        dict(samesite="invalid"), dict(samesite="None")]:
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    Response().set_cookie("token", "abc", **options)
        with self.assertRaises(ValueError):
            Response().set_cookie("label", "سڵاو")
        with self.assertRaises(ValueError):
            Response().set_cookie("label", "line\r\nInjected: value")
        with self.assertRaises(CookieError):
            Response().set_cookie("bad name", "value")

    async def test_samesite_none_with_secure(self):
        response = Response()
        response.set_cookie("token", "abc", secure=True, samesite="none")
        field = (await self.cookie_fields(response))[0]
        self.assertIn("SameSite=None", field)
        self.assertIn("Secure", field)

    async def test_cookies_work_on_response_subclasses(self):
        for response in [JSONResponse({"ok": True}), RedirectResponse("/notes")]:
            with self.subTest(response=type(response).__name__):
                response.set_cookie("token", "abc")
                headers = await self.headers_for(response)
                self.assertEqual(sum(name == b"set-cookie" for name, _ in headers), 1)
                if isinstance(response, RedirectResponse):
                    self.assertEqual(dict(headers)[b"location"], b"/notes")
