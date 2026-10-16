import unittest

from aurora.core.request import Request
from aurora.core.router import Router


class RouteURLContractTests(unittest.TestCase):
    def setUp(self):
        self.router = Router()
        self.route = self.router.add("/catalog/{item}", lambda request: None, name="catalog")

    def test_unicode_and_spaces_are_utf8_percent_encoded(self):
        self.assertEqual(self.router.url_for("catalog", item="café 東京"), "/catalog/caf%C3%A9%20%E6%9D%B1%E4%BA%AC")

    def test_reserved_characters_remain_within_single_segment(self):
        self.assertEqual(self.router.url_for("catalog", item="a?b#c&d=e"), "/catalog/a%3Fb%23c%26d%3De")

    def test_existing_percent_escapes_are_not_interpreted(self):
        self.assertEqual(self.router.url_for("catalog", item="%2F"), "/catalog/%252F")

    def test_url_generation_does_not_change_registration(self):
        before = (len(self.router.routes), len(self.router.named_routes))
        self.router.url_for("catalog", item="x+y")
        self.assertEqual((len(self.router.routes), len(self.router.named_routes)), before)

    def test_generated_ascii_url_matches_the_same_route(self):
        url = self.router.url_for("catalog", item="café & tea")
        request = Request({"path": url, "method": "GET"})
        self.assertEqual(self.route.matches(request), {"item": "caf%C3%A9%20%26%20tea"})


if __name__ == "__main__":
    unittest.main()
