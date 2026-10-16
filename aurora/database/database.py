from contextlib import contextmanager

from sqlalchemy import create_engine, text


class Database:
    """Execute SQL and SQLAlchemy Core statements through one engine.

    Operations reuse the connection held by transaction() while it is
    active. Transaction state belongs to this Database instance.
    """

    def __init__(self, url):
        self.url = url
        self.engine = create_engine(url)

        self._transaction_connection = None

    def connect(self):
        """Open a connection; the caller is responsible for closing it."""
        return self.engine.connect()

    @contextmanager
    def transaction(self):
        """Group operations using with db.transaction().

        Commit on normal exit and roll back on Exception. Nested
        transactions on this instance raise RuntimeError.
        """
        if self._transaction_connection is not None:
            raise RuntimeError("A database transaction is already active")

        connection = self.engine.connect()
        transaction = connection.begin()

        self._transaction_connection = connection

        try:
            yield
        except Exception:
            transaction.rollback()
            raise
        else:
            transaction.commit()
        finally:
            self._transaction_connection = None
            connection.close()

    def execute(self, statement, parameters=None):
        """Execute a SQL string with bound parameters and return its result.

        Outside transaction(), use a separate transaction for this call.
        """
        parameters = parameters or {}

        if self._transaction_connection is not None:
            return self._transaction_connection.execute(
                text(statement), parameters
            )

        with self.engine.begin() as connection:
            result = connection.execute(text(statement), parameters)

            return result

    def execute_statement(self, statement, parameters=None):
        """Execute a SQLAlchemy Core statement and return its result.

        Outside transaction(), use a separate transaction for this call.
        """
        parameters = parameters or {}

        if self._transaction_connection is not None:
            return self._transaction_connection.execute(statement, parameters)

        with self.engine.begin() as connection:
            result = connection.execute(statement, parameters)

            return result

    def execute_insert(self, statement, parameters=None):
        """Execute a Core insert and return its primary key as a tuple.

        The result is closed after reading the key.
        """
        parameters = parameters or {}

        if self._transaction_connection is not None:
            result = self._transaction_connection.execute(statement, parameters)

            try:
                return tuple(result.inserted_primary_key)
            finally:
                result.close()

        with self.engine.begin() as connection:
            result = connection.execute(statement, parameters)

            try:
                return tuple(result.inserted_primary_key)
            finally:
                result.close()

    def execute_write(self, statement, parameters=None):
        """Execute a Core write, close its result, and return its rowcount."""
        parameters = parameters or {}

        if self._transaction_connection is not None:
            result = self._transaction_connection.execute(statement, parameters)

            try:
                return result.rowcount
            finally:
                result.close()

        with self.engine.begin() as connection:
            result = connection.execute(statement, parameters)

            try:
                return result.rowcount
            finally:
                result.close()

    def fetch_all(self, statement, parameters=None):
        """Execute a Core statement and return row mappings, or an empty list."""
        parameters = parameters or {}

        if self._transaction_connection is not None:
            result = self._transaction_connection.execute(statement, parameters)

            try:
                return result.mappings().all()
            finally:
                result.close()

        with self.engine.connect() as connection:
            result = connection.execute(statement, parameters)

            try:
                return result.mappings().all()
            finally:
                result.close()

    def fetch_first(self, statement, parameters=None):
        """Execute a Core statement and return its first row mapping, or None.

        Close the result after reading; this method adds no SQL LIMIT.
        """
        parameters = parameters or {}

        if self._transaction_connection is not None:
            result = self._transaction_connection.execute(statement, parameters)

            try:
                return result.mappings().first()
            finally:
                result.close()

        with self.engine.connect() as connection:
            result = connection.execute(statement, parameters)

            try:
                return result.mappings().first()
            finally:
                result.close()

    def create_tables(self, metadata) -> None:
        """Create tables registered in metadata."""
        metadata.create_all(self.engine)

    def dispose(self) -> None:
        """Release database connection resources."""
        self.engine.dispose()
