"""Deterministic, non-executable migration review artifacts.

Review tokens prove document identity, not permission to modify a database.
No database connection or DDL is used by this module.
"""

import hashlib
import hmac
import json
from pathlib import Path

from .migration_format import migration_plan, validate_migration_plan

_FORMAT = "aurorasql.review.v1"


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def create_review(before, after, *, migration_id):
    """Create a stable, read-only proposal bound to both schema snapshots."""
    plan = migration_plan(before, after, migration_id=migration_id)
    document = {"format": _FORMAT, "before": before, "after": after,
                "plan": plan, "execution_allowed": False}
    digest = hashlib.sha256(_canonical(document)).hexdigest()
    return {**document, "review_sha256": digest}


def validate_review(review):
    """Reject tampering, mismatched snapshots, and execution permission."""
    required = {"format", "before", "after", "plan", "execution_allowed", "review_sha256"}

    if not isinstance(review, dict) or set(review) != required or review["format"] != _FORMAT:
        raise ValueError("Invalid migration review artifact")

    if review["execution_allowed"] is not False:
        raise ValueError("Migration review cannot authorize execution")

    plan = validate_migration_plan(review["plan"])
    expected = migration_plan(review["before"], review["after"], migration_id=plan["id"])

    if plan != expected:
        raise ValueError("Review plan differs from the snapshots")

    digest = review["review_sha256"]

    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Invalid review fingerprint")

    body = {key: review[key] for key in required if key != "review_sha256"}

    if not hmac.compare_digest(hashlib.sha256(_canonical(body)).hexdigest(), digest):
        raise ValueError("Review fingerprint mismatch")

    return review


def verify_review_acknowledgment(review, *, reviewed_sha256):
    """Confirm human-reviewed identity only; never grant DDL permission."""
    validate_review(review)

    if not isinstance(reviewed_sha256, str) or not hmac.compare_digest(
            review["review_sha256"], reviewed_sha256):
        raise PermissionError("Exact reviewed SHA-256 fingerprint required")

    return True


def review_json(review):
    validate_review(review)
    return json.dumps(review, sort_keys=True, indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"


def save_review(review, destination):
    """Explicitly create a new review file; never overwrite or create directories."""
    payload = review_json(review)
    path = Path(destination)

    if path.is_symlink():
        raise FileExistsError("Review destination must not be a symbolic link")

    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(payload)

    return path


def load_review(source):
    with Path(source).open("r", encoding="utf-8") as stream:
        return validate_review(json.load(stream))
