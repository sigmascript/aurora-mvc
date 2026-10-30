"""Opt-in direct JOIN API checks using disposable MySQL/PostgreSQL databases."""

import os
from uuid import uuid4

import pytest

from aurora.aurorasql import Model, column, connect, create_table


@pytest.mark.parametrize("dialect,environment", [
    ("mysql", "AURORASQL_TEST_MYSQL_URL"),
    ("postgresql", "AURORASQL_TEST_POSTGRESQL_URL"),
])
def test_direct_join_live_backends(dialect, environment):
    if os.getenv("AURORASQL_TEST_ALLOW_DDL") != "YES_DISPOSABLE_DB":
        pytest.skip("Requires explicit disposable-database DDL opt-in")
    url = os.getenv(environment)
    if not url:
        pytest.skip(f"{environment} not configured")

    suffix = uuid4().hex[:12]
    writers = f"direct_writers_{suffix}"
    stories = f"direct_stories_{suffix}"
    reviews = f"direct_reviews_{suffix}"

    Writer = type("Writer", (Model,), {
        "__table__": writers,
        "id": column(int, primary_key=True),
        "name": column(str, length=80),
    })
    Story = type("Story", (Model,), {
        "__table__": stories,
        "id": column(int, primary_key=True),
        "writer_id": column(int, references=f"{writers}.id"),
        "title": column(str, length=80),
    })
    Review = type("Review", (Model,), {
        "__table__": reviews,
        "id": column(int, primary_key=True),
        "story_id": column(int, references=f"{stories}.id"),
        "score": column(int),
    })

    with connect(url) as db:
        assert db.dialect == dialect
        created = []
        try:
            for model in (Writer, Story, Review):
                create_table(db, model)
                created.append(model.__table_name__)

            with db.transaction():
                Writer.using(db).create(id=1, name="Mira")
                Writer.using(db).create(id=2, name="Noor")
                Story.using(db).create(id=10, writer_id=1, title="First")
                Story.using(db).create(id=11, writer_id=2, title="Second")
                Review.using(db).create(id=100, story_id=10, score=4)
                Review.using(db).create(id=101, story_id=11, score=5)

            # Model names remain stable even when physical table names differ.
            rows = Story.using(db).join(
                models=[Writer, Review],
                cols=["Story.title", "Writer.name", "Review.score"],
                where={"Review.score--gt": 3},
                order_by={"Review.score": "DESC"},
                limit=1,
            )
            assert rows == [{
                "left__title": "Second", "right1__name": "Noor",
                "right2__score": 5,
            }]

            # Physical table references remain supported, too.
            rows = Story.using(db).join(
                models=[Writer],
                on=[("writer_id", "id")],
                cols=[f"{stories}.title", f"{writers}.name"],
                where={"Writer.name": "Mira"},
            )
            assert rows == [{"left__title": "First", "right__name": "Mira"}]

            # Nested filters, ordering and pagination.
            rows = Story.using(db).join(
                models=[Writer],
                cols=["Story.title", "Writer.name"],
                where={"$or": [{"Writer.name": "Mira"}, {"Writer.name": "Noor"}]},
                order_by={"Story.id": "ASC"},
                limit=1,
                offset=1,
            )
            assert rows == [{"left__title": "Second", "right__name": "Noor"}]

            assert Story.using(db).join(
                models=[Writer],
                where={"Writer.name": "Mira' OR 1=1 --"},
            ) == []

            with pytest.raises(ValueError):
                Story.using(db).join(models=[Writer], cols=["Writer.unknown"])
            with pytest.raises(ValueError):
                Story.using(db).join(models=[Writer], order_by={"Writer.name": "RANDOM"})
        finally:
            # Only tables created with this test's random suffix are removed.
            for table in reversed(created):
                quoted = f"`{table}`" if dialect == "mysql" else f'"{table}"'
                db.execute_write(f"DROP TABLE {quoted}")
