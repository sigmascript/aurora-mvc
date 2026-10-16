import tempfile
import unittest
from pathlib import Path

from sqlalchemy import MetaData

from aurora.database import (
    Database,
    Model,
    Query,
    column,
)


class TestQuery(unittest.TestCase):

    def setUp(self):
        self.original_metadata = (
            Model.metadata
        )

        self.original_database = (
            Model.database
        )

        Model.metadata = MetaData()
        Model.database = None

    def tearDown(self):
        Model.metadata = (
            self.original_metadata
        )

        Model.database = (
            self.original_database
        )

    def test_read_is_lazy(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        query = Note.read()

        self.assertIsInstance(
            query,
            Query,
        )

    def test_all_returns_model_instances(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                Model.bind(db)

                db.create_tables(
                    Model.metadata
                )

                Note.create(
                    title="First",
                )

                Note.create(
                    title="Second",
                )

                notes = Note.read().all()

                self.assertEqual(
                    len(notes),
                    2,
                )

                self.assertIsInstance(
                    notes[0],
                    Note,
                )

                self.assertIsInstance(
                    notes[1],
                    Note,
                )

                self.assertEqual(
                    {
                        note.title
                        for note in notes
                    },
                    {
                        "First",
                        "Second",
                    },
                )

            finally:
                db.dispose()

    def test_read_filters_by_equality(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

            published = column(
                bool,
                default=False,
            )

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                Model.bind(db)

                db.create_tables(
                    Model.metadata
                )

                Note.create(
                    title="Draft",
                    published=False,
                )

                Note.create(
                    title="Published",
                    published=True,
                )

                notes = Note.read(
                    where={
                        "published": True,
                    }
                ).all()

                self.assertEqual(
                    len(notes),
                    1,
                )

                self.assertEqual(
                    notes[0].title,
                    "Published",
                )

            finally:
                db.dispose()

    def test_read_supports_multiple_equalities(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

            published = column(bool)

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                Model.bind(db)

                db.create_tables(
                    Model.metadata
                )

                Note.create(
                    title="Aurora",
                    published=True,
                )

                Note.create(
                    title="Aurora",
                    published=False,
                )

                Note.create(
                    title="Other",
                    published=True,
                )

                notes = Note.read(
                    where={
                        "title": "Aurora",
                        "published": True,
                    }
                ).all()

                self.assertEqual(
                    len(notes),
                    1,
                )

                self.assertEqual(
                    notes[0].title,
                    "Aurora",
                )

                self.assertIs(
                    notes[0].published,
                    True,
                )

            finally:
                db.dispose()

    def test_first_returns_model_instance(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                Model.bind(db)

                db.create_tables(
                    Model.metadata
                )

                created = Note.create(
                    title="Aurora",
                )

                note = Note.read(
                    where={
                        "id": created.id,
                    }
                ).first()

                self.assertIsInstance(
                    note,
                    Note,
                )

                self.assertEqual(
                    note.id,
                    created.id,
                )

                self.assertEqual(
                    note.title,
                    "Aurora",
                )

            finally:
                db.dispose()

    def test_first_returns_none_when_no_record_matches(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                Model.bind(db)

                db.create_tables(
                    Model.metadata
                )

                note = Note.read(
                    where={
                        "id": 999,
                    }
                ).first()

                self.assertIsNone(
                    note
                )

            finally:
                db.dispose()

    def test_query_requires_database_when_executed(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

        query = Note.read()

        with self.assertRaises(RuntimeError):
            query.all()

    def test_query_reads_inside_active_transaction(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                Model.bind(db)

                db.create_tables(
                    Model.metadata
                )

                with db.transaction():
                    created = Note.create(
                        title="Uncommitted",
                    )

                    note = Note.read(
                        where={
                            "id": created.id,
                        }
                    ).first()

                    self.assertIsNotNone(
                        note
                    )

                    self.assertEqual(
                        note.title,
                        "Uncommitted",
                    )

            finally:
                db.dispose()


if __name__ == "__main__":
    unittest.main()
