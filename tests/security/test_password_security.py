"""Framework password helpers, including compatibility with the original password helper."""

import unittest

from aurora.security import (
    hash_password,
    hash_password_async,
    verify_password,
    verify_password_async,
)
from aurora.security.passwords import hash_password as legacy_hash


PASSWORD = "a secure test passphrase"


class TestPasswordSecurity(unittest.TestCase):
    def test_round_trip_and_wrong_password(self):
        stored = hash_password(PASSWORD)
        self.assertTrue(verify_password(PASSWORD, stored))
        self.assertFalse(verify_password("not the password", stored))

    def test_random_salts(self):
        self.assertNotEqual(hash_password(PASSWORD), hash_password(PASSWORD))

    def test_reject_invalid_input(self):
        for value in ("", "x" * 129, None, b"bytes"):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(ValueError):
                    hash_password(value)
                self.assertFalse(verify_password(value, "invalid"))

    def test_malformed_hash_is_rejected_without_derivation(self):
        for value in (None, "", "plaintext", "scrypt$999999999$8$1$ab$cd", "x" * 100000):
            with self.subTest(value=str(value)[:20]):
                self.assertFalse(verify_password(PASSWORD, value))

    def test_original_password_helper_hash_compatibility(self):
        stored = legacy_hash(PASSWORD)
        self.assertTrue(verify_password(PASSWORD, stored))
        self.assertTrue(__import__("aurora.security.passwords", fromlist=["verify_password"]).verify_password(PASSWORD, hash_password(PASSWORD)))


class TestAsyncPasswordSecurity(unittest.IsolatedAsyncioTestCase):
    async def test_async_round_trip(self):
        stored = await hash_password_async("unicode passphrase سڵاو")
        self.assertTrue(await verify_password_async("unicode passphrase سڵاو", stored))
        self.assertFalse(await verify_password_async("incorrect", stored))
