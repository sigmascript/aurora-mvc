"""Password hashing helpers using the Free Notes-compatible scrypt format.

Use async helpers inside async request handlers: scrypt is CPU/memory intensive.
"""

import asyncio
import hashlib
import secrets

SCRYPT_N = 2**17
SCRYPT_R = 8
SCRYPT_P = 1
MAX_PASSWORD_LENGTH = 128
_SALT_BYTES = 16
_HASH_BYTES = 32
_MAX_MEMORY = 256 * 1024 * 1024


def _valid_password(password):
    return isinstance(password, str) and 1 <= len(password) <= MAX_PASSWORD_LENGTH


def _derive(password, salt):
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_MAX_MEMORY,
        dklen=_HASH_BYTES,
    )


def hash_password(password: str) -> str:
    """Hash a password with a fresh salt; raises ValueError for invalid input."""
    if not _valid_password(password):
        raise ValueError("Password must contain 1 to 128 characters")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verify a supported scrypt hash; malformed/unknown formats return False.

    Work factors are fixed to prevent attacker-supplied hashes from raising cost.
    """
    if not _valid_password(password) or not isinstance(stored, str):
        return False
    # Bound parsing before any costly password derivation.
    if (
        len(stored)
        != len(f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}$")
        + 2 * (_SALT_BYTES + _HASH_BYTES)
        + 1
    ):
        return False
    try:
        algorithm, n, r, p, salt_hex, digest_hex = stored.split("$")
        if (algorithm, n, r, p) != ("scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P)):
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        if len(salt) != _SALT_BYTES or len(expected) != _HASH_BYTES:
            return False
    except ValueError:
        return False
    return secrets.compare_digest(_derive(password, salt), expected)


async def hash_password_async(password: str) -> str:
    """Hash a password in a worker thread to avoid blocking the event loop."""
    return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    """Verify a password in a worker thread to avoid blocking the event loop."""
    return await asyncio.to_thread(verify_password, password, stored)
