"""Explicit transaction-scoped SQLAlchemy Core access for a routed model.

No global Model.bind changes, no ambient shared transaction state, and no
cross-database transaction guarantees. Each context owns its connection.
"""
from contextlib import contextmanager
from sqlalchemy import text
from aurora.database.routed_access import RoutedDatabaseAccess


class RoutedTransaction:
    """Connection-bound operations; valid only inside the owning context."""

    def __init__(self, connection, access, alias):
        self._connection = connection
        self._access = access
        self._alias = alias
        self._active = True

    def _require_active(self):
        if not self._active:
            raise RuntimeError("Routed transaction is closed")
        return self._connection

    def model(self, model):
        """Bind a registered model to this transaction without global rebinding.

        Models sharing this database alias may be accessed in the same transaction.
        Models on another alias require their own transaction scope.
        """
        self._require_active()
        if self._access.alias_for(model) != self._alias:
            raise ValueError("Model targets a different database")
        if not isinstance(model, type) or not hasattr(model, "__sql_table__"):
            raise TypeError("Expected a registered Aurora model class")
        return RoutedModelOperations(self, model)

    def execute(self, statement, parameters=None):
        """Execute SQLAlchemy Core statement or parameterized SQL string."""
        connection = self._require_active()
        if isinstance(statement, str):
            statement = text(statement)
        return connection.execute(statement, parameters or {})

    def fetch_all(self, statement, parameters=None):
        result = self.execute(statement, parameters)
        try:
            return result.mappings().all()
        finally:
            result.close()

    def fetch_first(self, statement, parameters=None):
        result = self.execute(statement, parameters)
        try:
            return result.mappings().first()
        finally:
            result.close()

    def execute_write(self, statement, parameters=None):
        result = self.execute(statement, parameters)
        try:
            return result.rowcount
        finally:
            result.close()


@contextmanager
def routed_transaction(access: RoutedDatabaseAccess, model):
    """Open an independent transaction for one registered model's database.

    Commit on normal exit, roll back on errors. Never switch Model.database.
    Do not share this context or its connection between concurrent tasks.
    """
    if not isinstance(access, RoutedDatabaseAccess):
        raise TypeError("access must be a RoutedDatabaseAccess")
    database = access.database_for(model)
    with database.engine.begin() as connection:
        session = RoutedTransaction(connection, access, access.alias_for(model))
        try:
            yield session
        finally:
            session._active = False


class RoutedModelOperations:
    """Explicit synchronous model CRUD inside an owned routed transaction.

    Does not change Model.create/read/update/delete or Model.bind().
    """

    def __init__(self, transaction, model):
        self._transaction = transaction
        self._model = model

    def create(self, **values):
        tx = self._transaction
        tx._require_active()
        model = self._model
        model._validate_columns(values)
        result = tx.execute(model.__sql_table__.insert().values(**values))
        try:
            keys = tuple(result.inserted_primary_key)
        finally:
            result.close()
        data = dict(values)
        for col, value in zip(model.__sql_table__.primary_key.columns, keys):
            data.setdefault(col.name, value)
        for name, definition in model.__columns__.items():
            if name not in data and definition.default is not None:
                default = definition.default
                data[name] = default() if callable(default) else default
        return model(**data)

    def all(self, *, where=None):
        tx = self._transaction
        tx._require_active()
        model = self._model
        where = dict(where or {})
        model._validate_columns(where)
        stmt = model.__sql_table__.select()
        for name, value in where.items():
            stmt = stmt.where(model.__sql_table__.c[name] == value)
        return [model._from_row(row) for row in tx.fetch_all(stmt)]

    def first(self, *, where=None):
        tx = self._transaction
        tx._require_active()
        model = self._model
        where = dict(where or {})
        model._validate_columns(where)
        stmt = model.__sql_table__.select()
        for name, value in where.items():
            stmt = stmt.where(model.__sql_table__.c[name] == value)
        row = tx.fetch_first(stmt.limit(1))
        return model._from_row(row) if row is not None else None

    def update(self, *, values=None, where=None, confirm=False):
        tx = self._transaction
        tx._require_active()
        model = self._model
        values, where = dict(values or {}), dict(where or {})
        if not values:
            raise ValueError("Update values cannot be empty")
        model._validate_columns(values)
        model._validate_columns(where)
        model._require_write_scope(where=where, confirm=confirm)
        stmt = model.__sql_table__.update().values(**values)
        for name, value in where.items():
            stmt = stmt.where(model.__sql_table__.c[name] == value)
        return tx.execute_write(stmt)

    def delete(self, *, where=None, confirm=False):
        tx = self._transaction
        tx._require_active()
        model = self._model
        where = dict(where or {})
        model._validate_columns(where)
        model._require_write_scope(where=where, confirm=confirm)
        stmt = model.__sql_table__.delete()
        for name, value in where.items():
            stmt = stmt.where(model.__sql_table__.c[name] == value)
        return tx.execute_write(stmt)
