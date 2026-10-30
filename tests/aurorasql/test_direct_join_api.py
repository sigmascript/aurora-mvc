"""Direct JOIN API regression tests using generic models."""
import pytest
from aurora.aurorasql import Model, column, connect

class Writer(Model):
    __table__ = 'writers_v2'
    id = column(int, primary_key=True)
    name = column(str)

class Story(Model):
    __table__ = 'stories'
    id = column(int, primary_key=True)
    writer_id = column(int, references='writers_v2.id')
    title = column(str)

class Review(Model):
    __table__ = 'reviews'
    id = column(int, primary_key=True)
    story_id = column(int, references='stories.id')
    score = column(int)

@pytest.fixture
def db():
    with connect('sqlite:///:memory:') as connection:
        for sql in ('CREATE TABLE writers_v2 (id INTEGER PRIMARY KEY, name TEXT)',
                    'CREATE TABLE stories (id INTEGER PRIMARY KEY, writer_id INTEGER, title TEXT)',
                    'CREATE TABLE reviews (id INTEGER PRIMARY KEY, story_id INTEGER, score INTEGER)'):
            connection.execute_write(sql)
        with connection.transaction():
            Writer.using(connection).create(id=1, name='Mira')
            Writer.using(connection).create(id=2, name='Noor')
            Story.using(connection).create(id=10, writer_id=1, title='One')
            Story.using(connection).create(id=11, writer_id=2, title='Two')
            Review.using(connection).create(id=100, story_id=10, score=4)
            Review.using(connection).create(id=101, story_id=11, score=5)
        yield connection

def test_model_names_and_declared_relationships(db):
    rows = Story.using(db).join(
        models=[Writer, Review], cols=['Story.title', 'Writer.name', 'Review.score'],
        where={'Review.score--gt': 3}, order_by={'Review.score': 'DESC'}, limit=1)
    assert rows == [{'left__title': 'Two', 'right1__name': 'Noor', 'right2__score': 5}]

def test_physical_names_and_explicit_relationships(db):
    rows = Story.using(db).join(models=[Writer], on=[('writer_id', 'id')],
                                cols=['stories.title', 'writers_v2.name'],
                                where={'Writer.name': 'Mira'})
    assert rows == [{'left__title': 'One', 'right__name': 'Mira'}]

def test_reject_unknown_and_ambiguous_relationships(db):
    class Unrelated(Model):
        id = column(int, primary_key=True)
    with pytest.raises(ValueError, match='explicit on'):
        Story.using(db).join(models=[Unrelated])
    with pytest.raises(ValueError):
        Story.using(db).join(models=[Writer], cols=['Writer.unknown'])
    with pytest.raises(ValueError):
        Story.using(db).join(models=[Writer], order_by={'Writer.name': 'RANDOM'})

def test_filter_groups_and_offset(db):
    rows = Story.using(db).join(models=[Writer],
        cols=['Story.title', 'Writer.name'],
        where={'$or': [{'Writer.name': 'Mira'}, {'Writer.name': 'Noor'}]},
        order_by={'Story.id': 'ASC'}, limit=1, offset=1)
    assert rows == [{'left__title': 'Two', 'right__name': 'Noor'}]
