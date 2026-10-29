"""Experimental, connection-scoped CRUD models without SQLAlchemy.

Schema creation/migration is intentionally outside this module. Models use
existing tables and never implicitly modify database structure.
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime


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
        if python_type not in _TYPES:
            raise TypeError("Unsupported column type")
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
            if not self.nullable and not self.primary_key:
                raise ValueError("Non-nullable column cannot be None")
            return
        if self.python_type is int and type(value) is bool:
            raise TypeError("Boolean is not an integer column value")
        if self.python_type is float and type(value) is int:
            return
        if not isinstance(value, self.python_type):
            raise TypeError(f"Expected {self.python_type.__name__} column value")
        if self.length is not None and len(value) > self.length:
            raise ValueError("String exceeds column length")


def column(python_type, **options):
    return Column(python_type, **options)


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
            if field.python_type is bool and type(value) is int and value in (0, 1):
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
        """Build a lazy query with optional ordering and pagination."""
        return cls._operations().read(where, order_by=order_by, limit=limit, offset=offset)

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


def _normalize_ordering(order_by, columns):
    """Normalize safe, ordered column specifications into SQL directions."""
    if order_by is None:
        return None

    if isinstance(order_by, str):
        items = [order_by]
    elif isinstance(order_by, (list, tuple)):
        items = order_by
    elif isinstance(order_by, dict):
        items = list(order_by.items())
    else:
        raise ValueError("order_by must be a column, list, or direction dictionary")

    if not items:
        raise ValueError("order_by cannot be empty")

    ordering = []
    seen = set()
    for item in items:
        if isinstance(order_by, dict):
            name, direction = item
            if not isinstance(direction, str) or direction.upper() not in ("ASC", "DESC"):
                raise ValueError("Ordering direction must be ASC or DESC")
            descending = direction.upper() == "DESC"
        else:
            if not isinstance(item, str) or not item:
                raise ValueError("Ordering columns must be nonempty strings")
            descending = item.startswith("-")
            name = item[1:] if descending else item

        if name not in columns:
            raise ValueError(f"Unknown ordering column: {name!r}")
        if name in seen:
            raise ValueError(f"Duplicate ordering column: {name!r}")
        seen.add(name)
        ordering.append((name, descending))

    return tuple(ordering)


class Query:
    """Lazy equality-filter query with safe ordering and bounded pagination."""

    def __init__(self, operations, where=None, *, ordering=None, limit=None, offset=0, join=None):
        self._operations = operations
        self.where = dict(where or {})
        operations.model._validate(self.where)
        self._ordering = ordering
        self._limit = limit
        self._offset = offset
        self._join = join
        if ordering is not None:
            for name, descending in ordering:
                if name not in operations.model.__columns__:
                    raise ValueError(f"Unknown ordering column: {name!r}")
                if type(descending) is not bool:
                    raise TypeError("descending must be a boolean")
        if limit is not None and (type(limit) is not int or limit < 0):
            raise ValueError("limit must be a nonnegative integer")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a nonnegative integer")

    def order_by(self, name, *, descending=False):
        """Return a new query ordered by one declared model column."""
        if name not in self._operations.model.__columns__:
            raise ValueError(f"Unknown ordering column: {name!r}")
        if type(descending) is not bool:
            raise TypeError("descending must be a boolean")
        return Query(self._operations, self.where,
                     ordering=((name, descending),), limit=self._limit, offset=self._offset, join=self._join)

    def limit(self, count):
        """Return a new query with a nonnegative maximum row count."""
        if type(count) is not int or count < 0:
            raise ValueError("limit must be a nonnegative integer")
        return Query(self._operations, self.where,
                     ordering=self._ordering, limit=count, offset=self._offset, join=self._join)

    def offset(self, count):
        """Return a new query that skips rows; requires an explicit limit."""
        if type(count) is not int or count < 0:
            raise ValueError("offset must be a nonnegative integer")
        return Query(self._operations, self.where,
                     ordering=self._ordering, limit=self._limit, offset=count, join=self._join)

    def join(self, other_model, *, on):
        """Inner-join a declared model on two explicit columns.

        Joined results are dictionaries with ``left__`` and ``right__`` keys,
        rather than partially hydrated model objects. Only one join is supported.
        """
        if self._join is not None:
            raise ValueError("Only one join is supported")
        if not isinstance(other_model, type) or not issubclass(other_model, Model) or other_model is Model:
            raise TypeError("join requires a declared Model subclass")
        if other_model.__table_name__ == self._operations.model.__table_name__:
            raise ValueError("Joining a table to itself is not supported")
        if not isinstance(on, tuple) or len(on) != 2:
            raise TypeError("on must be a (left_column, right_column) tuple")
        left, right = on
        if left not in self._operations.model.__columns__ or right not in other_model.__columns__:
            raise ValueError("Join columns must be declared on their respective models")
        return Query(self._operations, self.where, ordering=self._ordering,
                     limit=self._limit, offset=self._offset,
                     join=(other_model, left, right))

    def _source(self):
        if self._join is None:
            return self._operations.table
        other, left, right = self._join
        quote = self._operations._quote
        return (f"{self._operations.table} AS {quote('left_row')} "
                f"INNER JOIN {quote(other.__table_name__)} AS {quote('right_row')} "
                f"ON {quote('left_row')}.{quote(left)} = "
                f"{quote('right_row')}.{quote(right)}")

    def _where(self):
        if self._join is None:
            return self._operations._where(self.where)
        clauses = []
        parameters = {}
        quote = self._operations._quote
        for index, (name, value) in enumerate(self.where.items()):
            identifier = f"{quote('left_row')}.{quote(name)}"
            if value is None:
                clauses.append(f"{identifier} IS NULL")
            else:
                key = f"w{index}"
                clauses.append(f"{identifier} = :{key}")
                parameters[key] = value
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters

    def _select(self, *, first=False):
        if self._offset and self._limit is None and not first:
            raise ValueError("offset requires limit")
        clause, parameters = self._where()
        quote = self._operations._quote
        if self._join is None:
            columns = "*"
        else:
            other, _, _ = self._join
            selected = []
            for prefix, model in (("left_row", self._operations.model), ("right_row", other)):
                for name in model.__columns__:
                    selected.append(
                        f"{quote(prefix)}.{quote(name)} AS {quote(prefix.split('_')[0] + '__' + name)}"
                    )
            columns = ", ".join(selected)
        sql = f"SELECT {columns} FROM {self._source()}{clause}"
        if self._ordering is not None:
            parts = []
            for name, descending in self._ordering:
                qualified = (f"{quote('left_row')}." if self._join else "") + quote(name)
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
        filters = dict(filters or {})
        self.model._validate(filters)
        clauses = []
        parameters = {}
        for index, (name, value) in enumerate(filters.items()):
            quoted = self._quote(name)
            if value is None:
                clauses.append(f"{quoted} IS NULL")
            else:
                key = f"w{index}"
                clauses.append(f"{quoted} = :{key}")
                parameters[key] = value
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), parameters

    def read(self, where=None, *, order_by=None, limit=None, offset=0):
        """Return a lazy query with safe single- or multi-column ordering.

        Accept a column string, ordered strings with '-' for DESC, or a
        dictionary mapping column names to ASC/DESC. Fluent JOIN remains.
        """
        ordering = _normalize_ordering(order_by, self.model.__columns__)
        return Query(self, where, ordering=ordering, limit=limit, offset=offset)

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
