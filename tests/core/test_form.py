import unittest
from urllib.parse import urlencode

from aurora import Application
from aurora.core.http_exception import HTTPException
from aurora.core.request import Request
from aurora.core.response import Response


class TestForm(unittest.IsolatedAsyncioTestCase):
    def make_request(self, body, content_type="application/x-www-form-urlencoded"):
        self.receive_calls = 0

        async def receive():
            self.receive_calls += 1
            return {"type": "http.request", "body": body, "more_body": False}

        headers = [] if content_type is None else [
            (b"content-type", content_type.encode("latin-1"))
        ]
        return Request({"method": "POST", "path": "/", "headers": headers}, receive)

    async def test_decodes_spaces_unicode_and_literal_symbols(self):
        expected = {"title": "سڵاو Aurora", "content": "a+b & c=1\nNext line"}
        request = self.make_request(urlencode(expected).encode("utf-8"))
        self.assertEqual(await request.form(), expected)

    async def test_retains_blank_fields_and_last_duplicate_value(self):
        request = self.make_request(b"title=&tag=first&tag=last&remember")
        self.assertEqual(await request.form(), {
            "title": "", "tag": "last", "remember": ""
        })

    async def test_empty_body_is_empty_form(self):
        request = self.make_request(b"")
        self.assertEqual(await request.form(), {})

    async def test_content_type_parameters_and_case(self):
        request = self.make_request(
            b"title=Hello", "Application/X-Www-Form-Urlencoded; charset=UTF-8"
        )
        self.assertEqual(await request.form(), {"title": "Hello"})

    async def test_body_is_reused_across_reads(self):
        request = self.make_request(b"title=Hello")
        self.assertEqual(await request.body(), b"title=Hello")
        self.assertEqual(await request.form(), {"title": "Hello"})
        self.assertEqual(await request.form(), {"title": "Hello"})
        self.assertEqual(self.receive_calls, 1)

    async def test_unsupported_or_missing_content_type_is_415(self):
        for content_type in [None, "application/json", "multipart/form-data"]:
            with self.subTest(content_type=content_type):
                request = self.make_request(b"title=Hello", content_type)
                with self.assertRaises(HTTPException) as error:
                    await request.form()
                self.assertEqual(error.exception.status_code, 415)
                self.assertEqual(self.receive_calls, 0)

    async def test_invalid_raw_or_percent_encoded_utf8_is_400(self):
        for body in [b"title=\xff", b"title=%FF"]:
            with self.subTest(body=body):
                request = self.make_request(body)
                with self.assertRaises(HTTPException) as error:
                    await request.form()
                self.assertEqual(error.exception.status_code, 400)
                self.assertEqual(error.exception.detail, "Invalid form data")

    async def test_field_count_limit(self):
        for count in [1000, 1001]:
            with self.subTest(count=count):
                body = "&".join(f"field{i}=value" for i in range(count)).encode()
                request = self.make_request(body)
                if count == 1000:
                    self.assertEqual(len(await request.form()), count)
                else:
                    with self.assertRaises(HTTPException) as error:
                        await request.form()
                    self.assertEqual(error.exception.status_code, 400)

    async def test_form_error_becomes_http_response(self):
        app = Application()

        async def submit(request):
            data = await request.form()
            return Response(data.get("title", ""))

        app.add_route("/", submit, methods=["POST"])
        request = self.make_request(b"title=%FF")
        messages = []

        async def send(message):
            messages.append(message)

        await app(request.scope | {"type": "http"}, request.receive, send)
        self.assertEqual(messages[0]["status"], 400)
        self.assertEqual(messages[1]["body"], b"Invalid form data")


if __name__ == "__main__":
    unittest.main()
