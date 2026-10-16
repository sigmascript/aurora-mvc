import tempfile
import unittest
from pathlib import Path

from sqlalchemy import (
    Integer,
    MetaData,
    String,
)

from aurora.database import (
    Database,
    Model,
    Query,
    column,
)


class TestModel(unittest.TestCase):

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

    def test_model_discovers_columns(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        self.assertEqual(
            list(Note.__columns__.keys()),
            [
                "id",
                "title",
            ],
        )

        self.assertIs(
            Note.__columns__["id"],
            Note.id,
        )

        self.assertIs(
            Note.__columns__["title"],
            Note.title,
        )

    def test_model_creates_sqlalchemy_table(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(
                str,
                length=200,
            )

        table = Note.__sql_table__

        self.assertEqual(
            table.name,
            "note",
        )

        self.assertEqual(
            list(table.columns.keys()),
            [
                "id",
                "title",
            ],
        )

        self.assertIsInstance(
            table.c.id.type,
            Integer,
        )

        self.assertIsInstance(
            table.c.title.type,
            String,
        )

        self.assertEqual(
            table.c.title.type.length,
            200,
        )

    def test_model_uses_snake_case_table_name(self):
        class BlogPost(Model):
            id = column(
                int,
                primary_key=True,
            )

        self.assertEqual(
            BlogPost.__table_name__,
            "blog_post",
        )

        self.assertEqual(
            BlogPost.__sql_table__.name,
            "blog_post",
        )

    def test_model_handles_acronym_in_name(self):
        class APIKey(Model):
            id = column(
                int,
                primary_key=True,
            )

        self.assertEqual(
            APIKey.__table_name__,
            "api_key",
        )

    def test_model_supports_explicit_table_name(self):
        class Note(Model):
            __table__ = "notes"

            id = column(
                int,
                primary_key=True,
            )

        self.assertEqual(
            Note.__table_name__,
            "notes",
        )

        self.assertEqual(
            Note.__sql_table__.name,
            "notes",
        )

    def test_model_preserves_column_options(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(
                str,
                length=200,
            )

            content = column(
                str,
                nullable=True,
            )

        table = Note.__sql_table__

        self.assertTrue(
            table.c.id.primary_key,
        )

        self.assertFalse(
            table.c.title.nullable,
        )

        self.assertTrue(
            table.c.content.nullable,
        )

    def test_models_share_metadata(self):
        class User(Model):
            id = column(
                int,
                primary_key=True,
            )

        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

        self.assertIs(
            User.__sql_table__.metadata,
            Model.metadata,
        )

        self.assertIs(
            Note.__sql_table__.metadata,
            Model.metadata,
        )

        self.assertIn(
            "user",
            Model.metadata.tables,
        )

        self.assertIn(
            "note",
            Model.metadata.tables,
        )

    def test_model_definition_does_not_require_database(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        self.assertEqual(
            Note.__table_name__,
            "note",
        )

        self.assertIn(
            "note",
            Model.metadata.tables,
        )

    def test_model_can_be_instantiated_with_values(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        note = Note(
            id=1,
            title="Aurora Next",
        )

        self.assertEqual(
            note.id,
            1,
        )

        self.assertEqual(
            note.title,
            "Aurora Next",
        )

    def test_model_rejects_unknown_values(self):
        class Note(Model):
            title = column(str)

        with self.assertRaises(TypeError):
            Note(
                unknown="value",
            )

    def test_model_requires_database_for_create(self):
        class Note(Model):
            title = column(str)

        with self.assertRaises(RuntimeError):
            Note.create(
                title="Aurora Next",
            )

    def test_model_create_inserts_and_returns_instance(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

            content = column(
                str,
                nullable=True,
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

                note = Note.create(
                    title="Aurora Next",
                    content="First real model record",
                )

                self.assertIsInstance(
                    note,
                    Note,
                )

                self.assertEqual(
                    note.id,
                    1,
                )

                self.assertEqual(
                    note.title,
                    "Aurora Next",
                )

                self.assertEqual(
                    note.content,
                    "First real model record",
                )

                result = db.execute(
                    """
                    SELECT id, title, content
                    FROM note
                    WHERE id = :id
                    """,
                    {
                        "id": note.id,
                    },
                )

                try:
                    row = result.first()

                    self.assertIsNotNone(
                        row,
                    )

                    self.assertEqual(
                        row[0],
                        1,
                    )

                    self.assertEqual(
                        row[1],
                        "Aurora Next",
                    )

                    self.assertEqual(
                        row[2],
                        "First real model record",
                    )

                finally:
                    result.close()

            finally:
                db.dispose()

    def test_model_create_rejects_unknown_values(self):
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

                with self.assertRaises(
                    TypeError
                ):
                    Note.create(
                        title="Aurora",
                        unknown="value",
                    )

            finally:
                db.dispose()

    def test_model_create_applies_default(self):
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

                note = Note.create(
                    title="Aurora Next",
                )

                self.assertIs(
                    note.published,
                    False,
                )

            finally:
                db.dispose()

    def test_model_read_returns_query(self):
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

        self.assertIs(
            query.model,
            Note,
        )

    def test_model_read_rejects_unknown_column(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with self.assertRaises(TypeError):
            Note.read(
                where={
                    "unknown": "value",
                }
            )

    def test_model_update_changes_matching_records(self):
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

                first = Note.create(
                    title="First",
                )

                second = Note.create(
                    title="Second",
                )

                affected = Note.update(
                    where={
                        "id": first.id,
                    },
                    values={
                        "title": "Updated",
                    },
                )

                self.assertEqual(
                    affected,
                    1,
                )

                updated = Note.read(
                    where={
                        "id": first.id,
                    }
                ).first()

                unchanged = Note.read(
                    where={
                        "id": second.id,
                    }
                ).first()

                self.assertEqual(
                    updated.title,
                    "Updated",
                )

                self.assertEqual(
                    unchanged.title,
                    "Second",
                )

            finally:
                db.dispose()

    def test_model_update_can_affect_multiple_records(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

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
                    published=False,
                )

                Note.create(
                    published=False,
                )

                Note.create(
                    published=True,
                )

                affected = Note.update(
                    where={
                        "published": False,
                    },
                    values={
                        "published": True,
                    },
                )

                self.assertEqual(
                    affected,
                    2,
                )

            finally:
                db.dispose()

    def test_model_update_rejects_empty_values(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

        with self.assertRaises(ValueError):
            Note.update(
                where={
                    "id": 1,
                },
                values={},
            )

    def test_model_update_rejects_unknown_column(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with self.assertRaises(TypeError):
            Note.update(
                where={
                    "id": 1,
                },
                values={
                    "unknown": "value",
                },
            )

    def test_model_update_requires_confirmation_without_where(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

            title = column(str)

        with self.assertRaises(RuntimeError):
            Note.update(
                values={
                    "title": "Changed",
                },
            )

    def test_model_update_allows_confirmed_whole_table_operation(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

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
                    published=True,
                )

                Note.create(
                    published=True,
                )

                affected = Note.update(
                    values={
                        "published": False,
                    },
                    confirm=True,
                )

                self.assertEqual(
                    affected,
                    2,
                )

            finally:
                db.dispose()

    def test_model_delete_removes_matching_records(self):
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

                first = Note.create(
                    title="First",
                )

                second = Note.create(
                    title="Second",
                )

                affected = Note.delete(
                    where={
                        "id": first.id,
                    }
                )

                self.assertEqual(
                    affected,
                    1,
                )

                deleted = Note.read(
                    where={
                        "id": first.id,
                    }
                ).first()

                remaining = Note.read(
                    where={
                        "id": second.id,
                    }
                ).first()

                self.assertIsNone(
                    deleted
                )

                self.assertIsNotNone(
                    remaining
                )

            finally:
                db.dispose()

    def test_model_delete_requires_confirmation_without_where(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

        with self.assertRaises(RuntimeError):
            Note.delete()

    def test_model_delete_empty_where_requires_confirmation(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

        with self.assertRaises(RuntimeError):
            Note.delete(
                where={},
            )

    def test_model_delete_allows_confirmed_whole_table_operation(self):
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

                affected = Note.delete(
                    confirm=True,
                )

                self.assertEqual(
                    affected,
                    2,
                )

                self.assertEqual(
                    Note.read().all(),
                    [],
                )

            finally:
                db.dispose()

    def test_whole_table_confirmation_must_be_true_boolean(self):
        class Note(Model):
            id = column(
                int,
                primary_key=True,
            )

        with self.assertRaises(RuntimeError):
            Note.delete(
                confirm="yes",
            )

    def test_model_operations_commit_inside_transaction(self):
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
                    first = Note.create(
                        title="First",
                    )

                    second = Note.create(
                        title="Second",
                    )

                    Note.update(
                        where={
                            "id": second.id,
                        },
                        values={
                            "title": "Updated",
                        },
                    )

                    found = Note.read(
                        where={
                            "id": first.id,
                        }
                    ).first()

                    self.assertIsNotNone(
                        found
                    )

                notes = Note.read().all()

                self.assertEqual(
                    len(notes),
                    2,
                )

                self.assertEqual(
                    {
                        note.title
                        for note in notes
                    },
                    {
                        "First",
                        "Updated",
                    },
                )

            finally:
                db.dispose()

    def test_model_operations_roll_back_together(self):
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

                with self.assertRaises(
                    RuntimeError
                ):
                    with db.transaction():
                        Note.create(
                            title="First",
                        )

                        Note.create(
                            title="Second",
                        )

                        raise RuntimeError(
                            "Cancel operation"
                        )

                notes = Note.read().all()

                self.assertEqual(
                    notes,
                    [],
                )

            finally:
                db.dispose()


if __name__ == "__main__":
    unittest.main()
