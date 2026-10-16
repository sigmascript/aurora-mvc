import unittest
from aurora import validate


class ValidationTests(unittest.TestCase):
    def test_required_email_and_minimum(self):
        errors = validate({"email": "bad", "password": "short"}, {
            "email": "required|email", "password": "required|min:8"
        })
        self.assertIn("email", errors)
        self.assertIn("password", errors)

    def test_valid_and_optional_empty(self):
        self.assertEqual(validate({"email": "a@example.com", "nickname": ""}, {
            "email": "required|email", "nickname": "min:3"
        }), {})

    def test_missing_and_whitespace(self):
        self.assertEqual(validate({}, {"name": "required"})["name"], ["This field is required."])
        self.assertIn("name", validate({"name": "   "}, {"name": "required"}))

    def test_maximum_and_unicode(self):
        self.assertIn("name", validate({"name": "abcd"}, {"name": "max:3"}))
        self.assertEqual(validate({"name": "éé"}, {"name": "min:2|max:2"}), {})

    def test_unknown_rules_rejected_even_on_empty_fields(self):
        for rule in ("unknown", "min:-1", "min:x", "max:", "required||email"):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                validate({}, {"field": rule})

    def test_non_text_input(self):
        self.assertIn("field", validate({"field": ["a"]}, {"field": "required"}))
