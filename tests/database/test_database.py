import tempfile
import unittest
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    inspect,
)

from aurora.database import (
    Database,
    Model,
    column,
)


class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.original_metadata = (
            Model.metadata
        )

        Model.metadata = MetaData()

    def tearDown(self):
        Model.metadata = (
            self.original_metadata
        )

    def test_database_creates_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                self.assertEqual(
                    db.url,
                    f"sqlite:///{database_path}",
                )

                self.assertIsNotNone(
                    db.engine,
                )

            finally:
                db.dispose()

    def test_database_can_connect(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                with db.connect() as connection:
                    self.assertIsNotNone(
                        connection,
                    )

            finally:
                db.dispose()

    def test_database_can_execute_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                db.execute(
                    """
                    CREATE TABLE notes (
                        id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL
                    )
                    """
                )

                db.execute(
                    """
                    INSERT INTO notes (title)
                    VALUES (:title)
                    """,
                    {
                        "title": "Aurora Next",
                    },
                )

                result = db.execute(
                    """
                    SELECT title
                    FROM notes
                    WHERE id = :id
                    """,
                    {
                        "id": 1,
                    },
                )

                try:
                    row = result.first()

                    self.assertIsNotNone(
                        row,
                    )

                    self.assertEqual(
                        row[0],
                        "Aurora Next",
                    )

                finally:
                    result.close()

            finally:
                db.dispose()

    def test_database_can_execute_core_statement(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                result = db.execute_statement(
                    notes.insert()
                )

                try:
                    self.assertEqual(
                        result.inserted_primary_key[
                            0
                        ],
                        1,
                    )

                finally:
                    result.close()

            finally:
                db.dispose()

    def test_database_execute_insert_returns_primary_key(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                primary_key = db.execute_insert(
                    notes.insert()
                )

                self.assertEqual(
                    primary_key,
                    (1,),
                )

            finally:
                db.dispose()

    def test_database_can_fetch_all_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                result = db.execute_statement(
                    notes.insert(),
                    [
                        {"id": 1},
                        {"id": 2},
                    ],
                )

                result.close()

                rows = db.fetch_all(
                    notes.select()
                )

                self.assertEqual(
                    len(rows),
                    2,
                )

                self.assertEqual(
                    rows[0]["id"],
                    1,
                )

                self.assertEqual(
                    rows[1]["id"],
                    2,
                )

            finally:
                db.dispose()

    def test_database_can_fetch_first_row(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                result = db.execute_statement(
                    notes.insert(),
                    [
                        {"id": 1},
                        {"id": 2},
                    ],
                )

                result.close()

                row = db.fetch_first(
                    notes.select()
                )

                self.assertIsNotNone(
                    row,
                )

                self.assertEqual(
                    row["id"],
                    1,
                )

            finally:
                db.dispose()

    def test_database_parameter_binding_handles_data_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                db.execute(
                    """
                    CREATE TABLE notes (
                        id INTEGER PRIMARY KEY,
                        title TEXT NOT NULL
                    )
                    """
                )

                title = (
                    "Aurora's \"simple\" database"
                )

                db.execute(
                    """
                    INSERT INTO notes (title)
                    VALUES (:title)
                    """,
                    {
                        "title": title,
                    },
                )

                result = db.execute(
                    """
                    SELECT title
                    FROM notes
                    WHERE id = :id
                    """,
                    {
                        "id": 1,
                    },
                )

                try:
                    row = result.first()

                    self.assertEqual(
                        row[0],
                        title,
                    )

                finally:
                    result.close()

            finally:
                db.dispose()

    def test_database_creates_model_tables(self):
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

        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                db.create_tables(
                    Model.metadata
                )

                inspector = inspect(
                    db.engine
                )

                self.assertIn(
                    "note",
                    inspector.get_table_names(),
                )

                columns = {
                    item["name"]: item
                    for item in inspector.get_columns(
                        "note"
                    )
                }

                self.assertEqual(
                    set(columns),
                    {
                        "id",
                        "title",
                        "content",
                    },
                )

                self.assertFalse(
                    columns["title"]["nullable"],
                )

                self.assertTrue(
                    columns["content"]["nullable"],
                )

            finally:
                db.dispose()

    def test_create_tables_handles_multiple_models(self):
        class User(Model):
            id = column(
                int,
                primary_key=True,
            )

            name = column(str)

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
                db.create_tables(
                    Model.metadata
                )

                inspector = inspect(
                    db.engine
                )

                table_names = set(
                    inspector.get_table_names()
                )

                self.assertIn(
                    "user",
                    table_names,
                )

                self.assertIn(
                    "note",
                    table_names,
                )

            finally:
                db.dispose()

    def test_database_execute_write_returns_affected_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                result = db.execute_statement(
                    notes.insert(),
                    [
                        {"id": 1},
                        {"id": 2},
                    ],
                )

                result.close()

                affected = db.execute_write(
                    notes.update()
                    .values(
                        id=10
                    )
                    .where(
                        notes.c.id == 1
                    )
                )

                self.assertEqual(
                    affected,
                    1,
                )

            finally:
                db.dispose()

    def test_transaction_commits_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                with db.transaction():
                    db.execute_insert(
                        notes.insert().values(
                            id=1
                        )
                    )

                row = db.fetch_first(
                    notes.select()
                    .where(
                        notes.c.id == 1
                    )
                )

                self.assertIsNotNone(
                    row
                )

            finally:
                db.dispose()

    def test_transaction_rolls_back_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                with self.assertRaises(
                    RuntimeError
                ):
                    with db.transaction():
                        db.execute_insert(
                            notes.insert().values(
                                id=1
                            )
                        )

                        raise RuntimeError(
                            "Stop transaction"
                        )

                row = db.fetch_first(
                    notes.select()
                    .where(
                        notes.c.id == 1
                    )
                )

                self.assertIsNone(
                    row
                )

            finally:
                db.dispose()

    def test_transaction_allows_reads_of_uncommitted_data(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            metadata = MetaData()

            notes = Table(
                "notes",
                metadata,
                Column(
                    "id",
                    Integer,
                    primary_key=True,
                ),
            )

            try:
                metadata.create_all(
                    db.engine
                )

                with db.transaction():
                    db.execute_insert(
                        notes.insert().values(
                            id=1
                        )
                    )

                    row = db.fetch_first(
                        notes.select()
                        .where(
                            notes.c.id == 1
                        )
                    )

                    self.assertIsNotNone(
                        row
                    )

            finally:
                db.dispose()

    def test_transaction_rejects_nested_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                with db.transaction():
                    with self.assertRaises(
                        RuntimeError
                    ):
                        with db.transaction():
                            pass

            finally:
                db.dispose()

    def test_transaction_state_clears_after_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                with db.transaction():
                    self.assertIsNotNone(
                        db._transaction_connection
                    )

                self.assertIsNone(
                    db._transaction_connection
                )

            finally:
                db.dispose()

    def test_transaction_state_clears_after_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = (
                Path(directory) / "test.db"
            )

            db = Database(
                f"sqlite:///{database_path}"
            )

            try:
                with self.assertRaises(
                    RuntimeError
                ):
                    with db.transaction():
                        raise RuntimeError(
                            "Rollback"
                        )

                self.assertIsNone(
                    db._transaction_connection
                )

            finally:
                db.dispose()


if __name__ == "__main__":
    unittest.main()
