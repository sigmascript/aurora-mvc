import re

from sqlalchemy import MetaData, Table

from aurora.database.column import Column
from aurora.database.query import Query


def _camel_to_snake(name: str) -> str:
    """Convert CamelCase names to snake_case."""
    name = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return name.lower()


class Model:
    """Declare model columns and perform database-backed CRUD operations.

    Subclasses register their directly declared Column attributes in
    Model.metadata. Table names default to the class name in snake_case;
    set __table__ on a subclass to override its name. Model.bind(database)
    configures the shared database used by all models.
    """

    metadata = MetaData()
    database = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        table_name = cls.__dict__.get(
            "__table__", _camel_to_snake(cls.__name__)
        )
        columns = {
            name: value
            for name, value in cls.__dict__.items()
            if isinstance(value, Column)
        }
        sql_columns = [
            definition.to_sqlalchemy(name)
            for name, definition in columns.items()
        ]

        cls.__table_name__ = table_name
        cls.__columns__ = columns
        cls.__sql_table__ = Table(table_name, Model.metadata, *sql_columns)

    def __init__(self, **values):
        self._set_values(values)

    @classmethod
    def bind(cls, database) -> None:
        """Set the shared database used by all Aurora models."""
        Model.database = database

    @classmethod
    def create(cls, **values):
        """Insert a record and return a model instance without reloading it.

        The returned instance includes supplied values, generated primary
        keys, and declared defaults for omitted fields. Callable defaults
        are evaluated again when constructing the returned instance.
        """
        database = cls._get_database()
        cls._validate_columns(values)

        statement = cls.__sql_table__.insert().values(**values)
        inserted_primary_key = database.execute_insert(statement)
        record_values = dict(values)
        primary_key_columns = list(cls.__sql_table__.primary_key.columns)

        for sql_column, value in zip(primary_key_columns, inserted_primary_key):
            if sql_column.name not in record_values:
                record_values[sql_column.name] = value

        for name, definition in cls.__columns__.items():
            if name not in record_values and definition.default is not None:
                default = definition.default

                if callable(default):
                    default = default()

                record_values[name] = default

        return cls(**record_values)

    @classmethod
    def read(cls, where=None) -> Query:
        """Create a lazy equality-filter query; call all() or first() to read."""
        return Query(model=cls, where=where)

    @classmethod
    def update(cls, *, values=None, where=None, confirm=False):
        """Update records matching all equality filters and return rowcount.

        Values must be non-empty. An empty filter requires confirm=True
        to allow a whole-table update.
        """
        values = dict(values or {})
        where = dict(where or {})

        if not values:
            raise ValueError("Update values cannot be empty")

        cls._validate_columns(values)
        cls._validate_columns(where)
        cls._require_write_scope(where=where, confirm=confirm)

        database = cls._get_database()
        statement = cls.__sql_table__.update().values(**values)

        for name, value in where.items():
            statement = statement.where(cls.__sql_table__.c[name] == value)

        return database.execute_write(statement)

    @classmethod
    def delete(cls, *, where=None, confirm=False):
        """Delete records matching all equality filters and return rowcount.

        An empty filter requires confirm=True to allow a whole-table delete.
        """
        where = dict(where or {})
        cls._validate_columns(where)
        cls._require_write_scope(where=where, confirm=confirm)

        database = cls._get_database()
        statement = cls.__sql_table__.delete()

        for name, value in where.items():
            statement = statement.where(cls.__sql_table__.c[name] == value)

        return database.execute_write(statement)

    @classmethod
    def _get_database(cls):
        """Return the shared database, or raise RuntimeError if unbound."""
        if Model.database is None:
            raise RuntimeError("No database is bound to Model")

        return Model.database

    @classmethod
    def _validate_columns(cls, values) -> None:
        """Reject unknown column names with TypeError."""
        for name in values:
            if name not in cls.__columns__:
                raise TypeError(f"Unknown column: {name}")

    @classmethod
    def _require_write_scope(cls, *, where, confirm) -> None:
        """Require explicit confirmation for unrestricted writes."""
        # Only the literal True authorizes a whole-table operation.
        if not where and confirm is not True:
            raise RuntimeError("Whole-table operation requires confirm=True")

    @classmethod
    def _from_row(cls, row):
        """Create a model instance from a database row mapping."""
        return cls(**dict(row))

    def _set_values(self, values) -> None:
        """Validate field names and assign the supplied values to this instance."""
        self._validate_columns(values)

        for name, value in values.items():
            setattr(self, name, value)
