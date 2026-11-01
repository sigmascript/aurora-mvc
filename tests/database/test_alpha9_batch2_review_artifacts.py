"""Batch 2: immutable migration review identity without database writes."""

import copy
import json

import pytest

from aurora.database import Model, column, schema_snapshot
from aurora.database.review_artifacts import (
    create_review, load_review, review_json, save_review,
    validate_review, verify_review_acknowledgment,
)


class Before(Model):
    __table__ = "review_people"
    id = column(int, primary_key=True)


class After(Model):
    __table__ = "review_people"
    id = column(int, primary_key=True)
    nickname = column(str, nullable=True)


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_deterministic_non_executable_review(dialect):
    before = schema_snapshot(Before, dialect=dialect)
    after = schema_snapshot(After, dialect=dialect)
    a = create_review(before, after, migration_id="review_001")
    b = create_review(before, after, migration_id="review_001")
    assert a == b
    assert a["execution_allowed"] is False
    assert a["plan"]["execution_allowed"] is False
    assert json.loads(review_json(a)) == a
    assert verify_review_acknowledgment(a, reviewed_sha256=a["review_sha256"])
    with pytest.raises(PermissionError):
        verify_review_acknowledgment(a, reviewed_sha256="0" * 64)


@pytest.mark.parametrize("change", [
    lambda r: r.update(execution_allowed=True),
    lambda r: r["plan"].update(execution_allowed=True),
    lambda r: r["plan"]["operations"][0].update(risk="potential_data_loss"),
    lambda r: r["after"]["tables"][0]["columns"][0].update(nullable=True),
    lambda r: r.update(review_sha256="0" * 64),
    lambda r: r.update(unexpected=True),
])
def test_tampering_refused(change):
    review = create_review(schema_snapshot(Before, dialect="sqlite"),
                           schema_snapshot(After, dialect="sqlite"), migration_id="review_001")
    damaged = copy.deepcopy(review)
    change(damaged)
    with pytest.raises(ValueError):
        validate_review(damaged)


def test_file_is_create_only_and_round_trips(tmp_path):
    review = create_review(schema_snapshot(Before, dialect="sqlite"),
                           schema_snapshot(After, dialect="sqlite"), migration_id="review_001")
    target = tmp_path / "proposal.json"
    save_review(review, target)
    assert load_review(target) == review
    with pytest.raises(FileExistsError):
        save_review(review, target)
    assert load_review(target) == review


def test_symlink_destination_refused(tmp_path):
    review = create_review(schema_snapshot(Before, dialect="sqlite"),
                           schema_snapshot(After, dialect="sqlite"), migration_id="review_001")
    existing = tmp_path / "existing"
    existing.write_text("untouched", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(existing)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this platform")
    with pytest.raises(FileExistsError):
        save_review(review, link)
    assert existing.read_text(encoding="utf-8") == "untouched"
