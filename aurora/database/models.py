"""Connection-scoped AuroraSQL models and query operations.

Schema creation/migration is intentionally outside this module. Models use
existing tables and never implicitly modify database structure.
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from decimal import Decimal

from .sql_types import parse_sql_type
from ._model_filters import _FILTER_OPERATORS, _compile_filters, _normalize_ordering
from ._model_join import execute_model_join


_TYPES = (str, int, float, bool, date, datetime)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUEST_CONNECTION = ContextVar("aurorasql_request_connection", default=None)


def _identifier(name):
    if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return '"' + name + '"'


class Column:
    """Describe a supported Python field; does not generate database DDL."""

    def __init__(self, python_type, *, primary_key=False, nullable=False,
                 default=None, length=None, unique=False,
                 server_default=None, references=None):
        self.sql_type = None
        if isinstance(python_type, str):
            self.sql_type, python_type = parse_sql_type(python_type)
        if python_type not in _TYPES and python_type is not Decimal:
            raise TypeError("Unsupported column type")
        if self.sql_type is not None and length is not None:
            raise ValueError("SQL type declarations already include their length")
        if length is not None and (python_type is not str or type(length) is not int or length <= 0):
            raise ValueError("length must be a positive integer for str fields")
        if type(primary_key) is not bool or type(nullable) is not bool or type(unique) is not bool:
            raise TypeError("Column flags must be booleans")
        self.python_type = python_type
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.length = length
        self.unique = unique
        if server_default is not None and not isinstance(server_default, str):
            raise TypeError("server_default must be a literal string or None")
        if references is not None:
            if not isinstance(references, str) or references.count(".") != 1:
                raise ValueError("references must use table.column syntax")
            table_name, column_name = references.split(".")
            _identifier(table_name)
            _identifier(column_name)
        self.server_default = server_default
        self.references = references
        if primary_key and nullable:
            raise ValueError("Primary-key columns cannot be nullable")
        if default is not None and not callable(default):
            self.validate(default)

    def validate(self, value):
        if value is None:
            if self.primary_key:
                raise ValueError("Primary-key column cannot be None")
            if not self.nullable:
                raise ValueError("Non-nullable column cannot be None")
            return
        if self.python_type is int and type(value) is bool:
            raise TypeError("Boolean is not an integer column value")
        if self.python_type is float and type(value) is int:
            return
        if not isinstance(value, self.python_type):
            raise TypeError(f"Expected {self.python_type.__name__} column value")
        max_length = self.length
        if self.sql_type and self.sql_type.startswith("VARCHAR("):
            max_length = int(self.sql_type[8:-1])
        if max_length is not None and len(value) > max_length:
            raise ValueError("String exceeds column length")
        if self.sql_type and self.sql_type.startswith("DECIMAL("):
            precision, scale = map(int, self.sql_type[8:-1].split(","))
            if not value.is_finite():
                raise ValueError("DECIMAL value must be finite")
            exponent = value.as_tuple().exponent
            if exponent < -scale or max(value.adjusted() + 1, 0) > precision - scale:
                raise ValueError("DECIMAL value exceeds declared precision or scale")


def column(python_type, **options):
    return Column(python_type, **options)


class ModelRow(dict):
    """Dictionary row with read-only attribute-style lookup compatibility."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class ModelResult(list):
    """Eager, list-compatible read/JOIN rows with safe in-memory summaries.

    Column references are resolved from declared model metadata, never SQL.
    A JOIN may project columns away; aggregating an absent column is refused.
    """

    def __init__(self, rows=(), *, columns=None):
        super().__init__(rows)
        self._columns = dict(columns or {})

    def all(self):
        return [ModelRow(row) if type(row) is dict else row for row in self]

    def first(self):
        row = self[0] if self else None
        return ModelRow(row) if type(row) is dict else row

    def last(self):
        row = self[-1] if self else None
        return ModelRow(row) if type(row) is dict else row

    def count(self):
        return len(self)

    def _values(self, column):
        if not isinstance(column, str):
            raise TypeError("Aggregate column must be a string")
        key = self._columns.get(column)
        if key is None:
            raise ValueError(f"Unknown or unavailable aggregate column: {column!r}")
        return [row[key] for row in self if row[key] is not None]

    def min(self, column):
        values = self._values(column)
        return min(values) if values else None

    def max(self, column):
        values = self._values(column)
        return max(values) if values else None

    def sum(self, column):
        values = self._values(column)
        if any(type(value) is bool or not isinstance(value, (int, float, Decimal))
               for value in values):
            raise TypeError("sum requires a numeric column")
        return sum(values) if values else None

    def avg(self, column):
        values = self._values(column)
        if any(type(value) is bool or not isinstance(value, (int, float, Decimal))
               for value in values):
            raise TypeError("avg requires a numeric column")
        return sum(values) / len(values) if values else None


class Model:
    """Declared model with Alpha 4-style binding and explicit .using(db) support."""

    database = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        fields = {name: value for name, value in cls.__dict__.items()
                  if isinstance(value, Column)}
        if not fields:
            raise ValueError("Model must declare at least one column")
        for name in fields:
            _identifier(name)
        table = cls.__dict__.get("__table__")
        if table is None:
            table = re.sub(r"(?<!^)(?=[A-Z])", "_", cls.__name__).lower()
        _identifier(table)
        cls.__columns__ = fields
        cls.__table_name__ = table

    def __init__(self, **values):
        self._validate(values)
        for name, value in values.items():
            setattr(self, name, value)

    @classmethod
    def _validate(cls, values):
        for name, value in values.items():
            if name not in cls.__columns__:
                raise TypeError(f"Unknown column: {name}")
            cls.__columns__[name].validate(value)

    @classmethod
    def _from_row(cls, row):
        """Normalize supported DB-API result types before model validation."""
        values = dict(row)
        for name, field in cls.__columns__.items():
            value = values.get(name)
            if field.python_type is Decimal and value is not None and not isinstance(value, Decimal):
                values[name] = Decimal(str(value))
            elif field.python_type is bool and type(value) is int and value in (0, 1):
                values[name] = bool(value)
            elif field.python_type is datetime and isinstance(value, str):
                values[name] = datetime.fromisoformat(value)
            elif field.python_type is date and isinstance(value, str):
                values[name] = date.fromisoformat(value)
        return cls(**values)

    @classmethod
    def bind(cls, connection):
        """Bind a shared connection, as in Aurora MVC Alpha 4.

        Call Model.bind(None) to clear the binding. Explicit .using(db)
        remains available for independent, connection-scoped operations.
        """
        Model.database = connection

    @classmethod
    @contextmanager
    def connection_scope(cls, connection):
        """Temporarily bind a connection to the current execution context.

        The caller owns the connection and must close it after the scope.
        Nested scopes restore the previous connection on exit. This is not
        a connection pool or an automatic ASGI request middleware.
        """
        if connection is None:
            raise ValueError("A connection is required")
        token = _REQUEST_CONNECTION.set(connection)
        try:
            yield connection
        finally:
            _REQUEST_CONNECTION.reset(token)

    @classmethod
    def _operations(cls):
        connection = _REQUEST_CONNECTION.get()
        if connection is None:
            connection = Model.database
        if connection is None:
            raise RuntimeError("No database is bound to Model")
        return cls.using(connection)

    @classmethod
    def read(cls, where=None, *, order_by=None, limit=None, offset=0):
        """Read plain dictionaries in a list-compatible ModelResult."""
        return cls._operations().read(where, order_by=order_by, limit=limit,
                                      offset=offset)

    @classmethod
    def query(cls, sql, parameters=None):
        """Execute parameterized SQL directly; return rows or affected count."""
        return cls._operations().query(sql, parameters)

    @classmethod
    def legacy_query(cls, where=None, *, order_by=None, limit=None, offset=0):
        """Temporary Alpha 8 lazy-query compatibility; slated for retirement."""
        return cls._operations().legacy_query(where, order_by=order_by,
                                              limit=limit, offset=offset)

    @classmethod
    def join(cls, models, *, cols=None, where=None, order_by=None,
             limit=None, offset=0, on=None):
        """Read related models directly, using declared relationships or explicit on."""
        return cls._operations().join(models, cols=cols, where=where,
                                      order_by=order_by, limit=limit,
                                      offset=offset, on=on)

    @classmethod
    def create(cls, **values):
        return cls._operations().create(**values)

    @classmethod
    def update(cls, *, values=None, where=None, confirm=False):
        return cls._operations().update(values=values, where=where, confirm=confirm)

    @classmethod
    def delete(cls, *, where=None, confirm=False):
        return cls._operations().delete(where=where, confirm=confirm)

    @classmethod
    def using(cls, connection):
        """Return operations bound to one explicit AuroraSQL connection."""
        return ModelOperations(cls, connection)


class Query:
    """Lazy validated-filter query with safe ordering and bounded pagination."""

    def __init__(self, operations, where=None, *, ordering=None, limit=None, offset=0, join=None, selected=None):
        self._operations = operations
        self.where = dict(where or {})
        if join is None:
            _compile_filters(self.where, operations.model.__columns__, operations._quote)
        self._selected = selected
        self._ordering = ordering
        self._limit = limit
        self._offset = offset
        self._join = join
        if join is not None:
            self._where()
        if ordering is not None:
            for name, descending in ordering:
                if name not in self._available_columns():
                    raise ValueError(f"Unknown ordering column: {name!r}")
                if type(descending) is not bool:
                    raise TypeError("descending must be a boolean")
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")

    def order_by(self, name, *, descending=False):
        """Return a new query ordered by one declared model column."""
        if name not in self._available_columns():
            raise ValueError(f"Unknown ordering column: {name!r}")
        if type(descending) is not bool:
            raise TypeError("descending must be a boolean")
        return Query(self._operations, self.where,
                     ordering=((name, descending),), limit=self._limit, offset=self._offset, join=self._join, selected=self._selected)

    def limit(self, count):
        """Return a new query with a nonnegative maximum row count."""
        if type(count) is not int or count < 0:
            raise ValueError("limit must be a nonnegative integer")
        return Query(self._operations, self.where,
                     ordering=self._ordering, limit=count, offset=self._offset, join=self._join, selected=self._selected)

    def offset(self, count):
        """Return a new query that skips rows; requires an explicit limit."""
        if type(count) is not int or count < 0:
            raise ValueError("offset must be a nonnegative integer")
        return Query(self._operations, self.where,
                     ordering=self._ordering, limit=self._limit, offset=count, join=self._join, selected=self._selected)

    def _models(self):
        return (self._operations.model,) + tuple(step[0] for step in (self._join or ()))

    def _available_columns(self):
        models = self._models()
        if self._join is None:
            return models[0].__columns__
        columns = {}
        for model in models:
            for name, field in model.__columns__.items():
                columns[f"{model.__table_name__}.{name}"] = field
        for name, field in models[0].__columns__.items():
            columns[name] = field
        return columns

    def _qualified(self, name):
        models = self._models()
        if name in models[0].__columns__:
            return f"{self._operations._quote('left_row')}.{self._operations._quote(name)}"
        if not isinstance(name, str) or name.count('.') != 1:
            raise ValueError(f"Unknown qualified column: {name!r}")
        table, field = name.split('.')
        for index, model in enumerate(models):
            if model.__table_name__ == table and field in model.__columns__:
                alias = 'left_row' if index == 0 else f'right_row_{index}'
                return f"{self._operations._quote(alias)}.{self._operations._quote(field)}"
        raise ValueError(f"Unknown qualified column: {name!r}")

    def join(self, other_model, *, on):
        """Add an explicit inner join. Qualified left columns use table.column."""
        if not isinstance(other_model, type) or not issubclass(other_model, Model) or other_model is Model:
            raise TypeError("join requires a declared Model subclass")
        if other_model.__table_name__ in {model.__table_name__ for model in self._models()}:
            raise ValueError("Only one join per table is supported; joining a table to itself is not supported")
        if not isinstance(on, tuple) or len(on) != 2:
            raise TypeError("on must be a (left_column, right_column) tuple")
        left, right = on
        if left not in self._available_columns() or right not in other_model.__columns__:
            raise ValueError("Join columns must be declared on their respective models")
        return Query(self._operations, self.where, ordering=self._ordering,
                     limit=self._limit, offset=self._offset,
                     join=(self._join or ()) + ((other_model, left, right),),
                     selected=self._selected)

    def where_joined(self, where):
        """Replace filters on a joined query using validated qualified fields."""
        if self._join is None:
            raise ValueError("where_joined requires a join")
        return Query(self._operations, where, ordering=self._ordering,
                     limit=self._limit, offset=self._offset,
                     join=self._join, selected=self._selected)

    def select(self, *columns):
        """Select explicit validated columns from a joined query."""
        if self._join is None:
            raise ValueError("Projection currently requires a join")
        if not columns or len(set(columns)) != len(columns):
            raise ValueError("Selected columns must be nonempty and unique")
        for name in columns:
            if name not in self._available_columns():
                raise ValueError(f"Unknown selected column: {name!r}")
        return Query(self._operations, self.where, ordering=self._ordering,
                     limit=self._limit, offset=self._offset,
                     join=self._join, selected=tuple(columns))

    def _source(self):
        if self._join is None:
            return self._operations.table
        quote = self._operations._quote
        sql = f"{self._operations.table} AS {quote('left_row')}"
        for index, (model, left, right) in enumerate(self._join, 1):
            alias = f'right_row_{index}'
            sql += (f" INNER JOIN {quote(model.__table_name__)} AS {quote(alias)}"
                    f" ON {self._qualified(left)} = {quote(alias)}.{quote(right)}")
        return sql

    def _where(self):
        if self._join is None:
            return self._operations._where(self.where)
        columns = self._available_columns()
        identifiers = {name: self._qualified(name) for name in columns}
        return _compile_filters(self.where, columns, self._operations._quote,
                                identifiers=identifiers)

    def _select(self, *, first=False):
        if self._offset and self._limit is None and not first:
            raise ValueError("offset requires limit")
        clause, parameters = self._where()
        quote = self._operations._quote
        if self._join is None:
            columns = "*"
        else:
            selected = []
            models = self._models()
            if self._selected is None:
                names = [f"{model.__table_name__}.{name}"
                         for model in models for name in model.__columns__]
            else:
                names = self._selected
            for name in names:
                if '.' in name:
                    table, field = name.split('.')
                    index = next(i for i, model in enumerate(models)
                                 if model.__table_name__ == table)
                else:
                    field = name
                    index = 0
                alias = ('left' if index == 0 else
                         'right' if len(models) == 2 else f'right{index}')
                selected.append(f"{self._qualified(name)} AS {quote(alias + '__' + field)}")
            columns = ", ".join(selected)
        sql = f"SELECT {columns} FROM {self._source()}{clause}"
        if self._ordering is not None:
            parts = []
            for name, descending in self._ordering:
                qualified = self._qualified(name) if self._join else quote(name)
                parts.append(f"{qualified} {'DESC' if descending else 'ASC'}")
            sql += " ORDER BY " + ", ".join(parts)
        count = 1 if first and self._limit is None else self._limit
        if count is not None:
            sql += " LIMIT :_query_limit"
            parameters["_query_limit"] = min(count, 1) if first else count
        if self._offset:
            sql += " OFFSET :_query_offset"
            parameters["_query_offset"] = self._offset
        return sql, parameters

    def count(self):
        """Count matching rows (joined rows when a join is present)."""
        clause, parameters = self._where()
        row = self._operations.connection.fetch_first(
            f"SELECT COUNT(*) AS row_count FROM {self._source()}{clause}", parameters)
        return int(row["row_count"])

    def exists(self):
        """Check for a matching row without materializing models."""
        clause, parameters = self._where()
        row = self._operations.connection.fetch_first(
            f"SELECT 1 AS present FROM {self._source()}{clause} LIMIT 1", parameters)
        return row is not None

    def _materialize(self, row):
        if self._join is not None:
            return dict(row)
        return self._operations.model._from_row(row)

    def all(self):
        sql, parameters = self._select()
        rows = self._operations.connection.fetch_all(sql, parameters)
        return [self._materialize(row) for row in rows]

    def first(self):
        sql, parameters = self._select(first=True)
        row = self._operations.connection.fetch_first(sql, parameters)
        return None if row is None else self._materialize(row)


class ModelOperations:
    """Basic CRUD against an existing table, scoped to a connection."""

    def __init__(self, model, connection):
        self.model = model
        self.connection = connection
        self.table = self._quote(model.__table_name__)

    def _quote(self, name):
        _identifier(name)
        if self.connection.dialect == "mysql":
            return "`" + name + "`"
        return _identifier(name)

    def _where(self, filters):
        return _compile_filters(filters, self.model.__columns__, self._quote)

    def query(self, sql, parameters=None):
        """Run SQL on this model's bound connection with named value binding."""
        return self.connection.execute(sql, parameters)

    def legacy_query(self, where=None, *, order_by=None, limit=None, offset=0):
        """Return the temporary validated lazy query for migration callers."""
        ordering = _normalize_ordering(order_by, self.model.__columns__)
        return Query(self, where, ordering=ordering, limit=limit, offset=offset)

    def read(self, where=None, *, order_by=None, limit=None, offset=0):
        """Execute a read and return plain dictionaries with normalized values."""
        query = self.legacy_query(where, order_by=order_by, limit=limit, offset=offset)
        rows = [vars(row) for row in query.all()]
        columns = {name: name for name in self.model.__columns__}
        for prefix in (self.model.__name__, self.model.__table_name__):
            columns.update({f"{prefix}.{name}": name for name in self.model.__columns__})
        return ModelResult(rows, columns=columns)

    def join(self, models, *, cols=None, where=None, order_by=None,
             limit=None, offset=0, on=None):
        """Execute a direct INNER JOIN using declared or explicit relationships."""
        return execute_model_join(
            self, models, cols=cols, where=where, order_by=order_by,
            limit=limit, offset=offset, on=on,
        )

    def all(self, *, where=None):
        clause, parameters = self._where(where)
        rows = self.connection.fetch_all(f"SELECT * FROM {self.table}{clause}", parameters)
        return [self.model._from_row(row) for row in rows]

    def first(self, *, where=None):
        clause, parameters = self._where(where)
        row = self.connection.fetch_first(
            f"SELECT * FROM {self.table}{clause} LIMIT 1", parameters)
        return None if row is None else self.model._from_row(row)

    def create(self, **values):
        """Insert a record and populate an omitted integer primary key.

        Generated-key retrieval uses the same connection as the insert. It is
        not safe to share a Connection concurrently between tasks or threads.
        """
        values = dict(values)
        self.model._validate(values)
        for name, field in self.model.__columns__.items():
            if name not in values and field.default is not None:
                values[name] = field.default() if callable(field.default) else field.default
        self.model._validate(values)

        keys = [(name, field) for name, field in self.model.__columns__.items()
                if field.primary_key]
        generated = len(keys) == 1 and keys[0][1].python_type is int and keys[0][0] not in values
        if values:
            columns = ", ".join(self._quote(name) for name in values)
            placeholders = ", ".join(f":v{index}" for index in range(len(values)))
            parameters = {f"v{index}": value for index, value in enumerate(values.values())}
            statement = f"INSERT INTO {self.table} ({columns}) VALUES ({placeholders})"
        else:
            statement = (f"INSERT INTO {self.table} () VALUES ()" if self.connection.dialect == "mysql"
                         else f"INSERT INTO {self.table} DEFAULT VALUES")
            parameters = {}

        if generated and self.connection.dialect == "postgresql":
            key_name = keys[0][0]
            rows = self.connection.fetch_all(
                statement + f" RETURNING {self._quote(key_name)}", parameters)
            if len(rows) != 1:
                raise RuntimeError("Insert did not return exactly one generated key")
            values[key_name] = rows[0][key_name]
        else:
            self.connection.execute_write(statement, parameters)
            if generated:
                key_name = keys[0][0]
                key_sql = ("SELECT LAST_INSERT_ID() AS generated_key" if self.connection.dialect == "mysql"
                           else "SELECT last_insert_rowid() AS generated_key")
                row = self.connection.fetch_first(key_sql)
                if row is None or row["generated_key"] is None:
                    raise RuntimeError("Generated primary key unavailable")
                values[key_name] = row["generated_key"]
        return self.model(**values)

    def update(self, *, values=None, where=None, confirm=False):
        values = dict(values or {})
        if not values:
            raise ValueError("Update values cannot be empty")
        self.model._validate(values)
        clause, parameters = self._where(where)
        if not clause and confirm is not True:
            raise RuntimeError("Whole-table operation requires confirm=True")
        assignments = []
        for index, (name, value) in enumerate(values.items()):
            key = f"v{index}"
            assignments.append(f"{self._quote(name)} = :{key}")
            parameters[key] = value
        return self.connection.execute_write(
            f"UPDATE {self.table} SET {', '.join(assignments)}{clause}", parameters)

    def delete(self, *, where=None, confirm=False):
        clause, parameters = self._where(where)
        if not clause and confirm is not True:
            raise RuntimeError("Whole-table operation requires confirm=True")
        return self.connection.execute_write(f"DELETE FROM {self.table}{clause}", parameters)
