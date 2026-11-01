"""Experimental synchronous DB-API core for AuroraSQL.

Independent of the released SQLAlchemy-backed database implementation.
Connections and transaction ownership must never be shared across them.
"""

from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
import importlib
import sqlite3
from urllib.parse import unquote, urlsplit


def _compile_sql(sql, parameters, style):
    """Bind :name placeholders without rewriting quoted SQL or comments.

    Only named parameters are supported. Values are always passed to the
    database driver, never interpolated into the SQL string.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("SQL must be a nonempty string")
    if not isinstance(parameters, dict):
        raise TypeError("Parameters must be a dictionary")
    if style not in ("named", "pyformat"):
        raise ValueError("Unsupported parameter style")

    output = []
    used = set()
    i = 0
    size = len(sql)
    state = "code"
    dollar_tag = None

    while i < size:
        ch = sql[i]
        next_ch = sql[i + 1] if i + 1 < size else ""

        if state == "code":
            if ch == "'":
                state = "single"
            elif ch == '"':
                state = "double"
            elif ch == "`":
                state = "backtick"
            elif ch == "-" and next_ch == "-":
                output.append("--")
                i += 2
                state = "line_comment"
                continue
            elif ch == "#" and style == "pyformat":
                state = "line_comment"
            elif ch == "/" and next_ch == "*":
                output.append("/*")
                i += 2
                state = "block_comment"
                continue
            elif ch == "$":
                end = sql.find("$", i + 1)
                if end != -1:
                    tag = sql[i:end + 1]
                    if tag == "$$" or (tag[1:-1].replace("_", "a").isalnum() and
                                         (tag[1].isalpha() or tag[1] == "_")):
                        dollar_tag = tag
                        state = "dollar"
                        output.append(tag)
                        i = end + 1
                        continue
            elif ch == ":" and next_ch == ":":
                output.append("::")
                i += 2
                continue
            elif ch == ":" and (next_ch.isalpha() or next_ch == "_") and next_ch.isascii() and (i == 0 or sql[i - 1] != ":"):
                end = i + 2
                while end < size and sql[end].isascii() and (sql[end].isalnum() or sql[end] == "_"):
                    end += 1
                name = sql[i + 1:end]
                used.add(name)
                output.append(":" + name if style == "named" else "%(" + name + ")s")
                i = end
                continue
            if ch == "%" and style == "pyformat":
                output.append("%%")
                i += 1
                continue
        elif state in ("single", "double", "backtick"):
            quote = {"single": "'", "double": '"', "backtick": "`"}[state]
            if ch == "\\" and next_ch:
                output.append(ch + next_ch)
                i += 2
                continue
            if ch == quote:
                if next_ch == quote:
                    output.append(ch + next_ch)
                    i += 2
                    continue
                state = "code"
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "*" and next_ch == "/":
                output.append("*/")
                i += 2
                state = "code"
                continue
        elif state == "dollar":
            if sql.startswith(dollar_tag, i):
                output.append(dollar_tag)
                i += len(dollar_tag)
                state = "code"
                dollar_tag = None
                continue
        output.append(ch)
        i += 1

    if state not in ("code", "line_comment"):
        raise ValueError("Unterminated SQL literal or comment")
    missing = used - parameters.keys()
    if missing:
        raise ValueError("Missing SQL parameter(s): " + ", ".join(sorted(missing)))
    extra = parameters.keys() - used
    if extra:
        raise ValueError("Unused SQL parameter(s): " + ", ".join(sorted(extra)))
    return "".join(output), {name: parameters[name] for name in used}


def _sqlite_parameters(parameters):
    """Adapt temporal values locally, without deprecated global sqlite adapters.

    ISO strings round-trip through Model._from_row, while raw SQL callers
    receive SQLite's native TEXT values. Other dialects retain driver handling.
    """
    return {
        key: value.isoformat(sep=" ") if isinstance(value, datetime)
        else value.isoformat() if isinstance(value, date)
        else str(value) if isinstance(value, Decimal)
        else value
        for key, value in parameters.items()
    }


class QueryResult:
    """Cursor-like result for Legacy SQL callers, without holding an open cursor.

    Rows are materialized when the statement executes. Fetch methods consume
    that in-memory result, matching the ordinary DB-API fetch pattern.
    """

    def __init__(self, result):
        self._rows = result if isinstance(result, list) else None
        self._position = 0
        self.rowcount = len(result) if isinstance(result, list) else result

    def _require_rows(self):
        if self._rows is None:
            raise ValueError("Statement did not return rows")

    def fetchall(self):
        self._require_rows()
        rows = self._rows[self._position:]
        self._position = len(self._rows)
        return rows

    def fetchone(self):
        self._require_rows()
        if self._position >= len(self._rows):
            return None
        row = self._rows[self._position]
        self._position += 1
        return row


class Connection:
    """Owns a single DB-API connection and its explicit transactions."""

    def __init__(self, raw, dialect):
        self._raw = raw
        self.dialect = dialect
        self._transaction_active = False
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise RuntimeError("Database connection is closed")

    def query(self, sql, parameters=None):
        """Execute bound SQL; expose Legacy-style .fetchall()/.fetchone().

        This compatibility result does not retain an open database cursor.
        Use :name placeholders and a parameters dictionary for dynamic values.
        """
        return QueryResult(self.execute(sql, parameters))

    def fetch_all(self, sql, parameters=None):
        """Return rows as dictionaries; reject statements without a result set."""
        rows = self.execute(sql, parameters)
        if not isinstance(rows, list):
            raise ValueError("Statement did not return rows")
        return rows

    def fetch_first(self, sql, parameters=None):
        """Return the first row or None (without changing SQL semantics)."""
        self._ensure_open()
        statement, values = _compile_sql(
            sql, {} if parameters is None else parameters,
            "named" if self.dialect == "sqlite" else "pyformat",
        )
        if self.dialect == "sqlite":
            values = _sqlite_parameters(values)
        cursor = self._raw.cursor()
        try:
            cursor.execute(statement, values)
            if cursor.description is None:
                raise ValueError("Statement did not return rows")
            row = cursor.fetchone()
            if row is None:
                return None
            return dict(zip((item[0] for item in cursor.description), row))
        finally:
            cursor.close()

    def execute_write(self, sql, parameters=None):
        """Return affected row count; reject statements that return rows."""
        result = self.execute(sql, parameters)
        if isinstance(result, list):
            raise ValueError("Statement returned rows; use fetch_all instead")
        return result

    def execute(self, sql, parameters=None):
        self._ensure_open()
        statement, values = _compile_sql(
            sql, {} if parameters is None else parameters,
            "named" if self.dialect == "sqlite" else "pyformat",
        )
        if self.dialect == "sqlite":
            values = _sqlite_parameters(values)
        cursor = self._raw.cursor()
        try:
            cursor.execute(statement, values)
            if cursor.description is not None:
                columns = [item[0] for item in cursor.description]
                return [dict(zip(columns, row)) for row in cursor.fetchall()]
            return cursor.rowcount
        finally:
            cursor.close()

    @contextmanager
    def transaction(self):
        self._ensure_open()
        if self._transaction_active:
            raise RuntimeError("Nested transactions are not supported")
        self._transaction_active = True
        started = False
        try:
            cursor = self._raw.cursor()
            try:
                cursor.execute("BEGIN")
                started = True
            finally:
                cursor.close()
            try:
                yield self
            except BaseException:
                self._raw.rollback()
                raise
            else:
                try:
                    self._raw.commit()
                except BaseException:
                    # A failed commit has an uncertain outcome. Attempt cleanup,
                    # but never silently retry or report success.
                    try:
                        self._raw.rollback()
                    except BaseException:
                        pass
                    raise
        except BaseException:
            if not started:
                try:
                    self._raw.rollback()
                except BaseException:
                    pass
            raise
        finally:
            self._transaction_active = False

    def close(self):
        if not self._closed:
            self._closed = True
            try:
                if self._transaction_active:
                    self._raw.rollback()
            finally:
                self._raw.close()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def connect(url):
    """Open a standalone DB-API connection; never migrate or retry implicitly."""
    if not isinstance(url, str):
        raise TypeError("Database URL must be a string")
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme in ("sqlite", "sqlite+pysqlite"):
        if parsed.netloc:
            raise ValueError("SQLite URL must not contain a hostname")
        if parsed.query or parsed.fragment:
            raise ValueError("SQLite URL query and fragment are not supported")
        path = unquote(parsed.path)
        if path == "/:memory:":
            database = ":memory:"
        elif path.startswith("///"):
            database = path[2:]
        elif path.startswith("//"):
            database = path[1:]
        elif path.startswith("/"):
            database = path[1:]
        else:
            raise ValueError("Invalid SQLite database path")
        raw = sqlite3.connect(database, isolation_level=None)
        try:
            raw.execute("PRAGMA foreign_keys = ON")
        except BaseException:
            raw.close()
            raise
        return Connection(raw, "sqlite")
    if scheme not in ("mysql+pymysql", "postgresql+psycopg"):
        raise ValueError("Unsupported AuroraSQL driver")
    if parsed.query or parsed.fragment:
        raise ValueError("Database URL query and fragment are not yet supported")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError("Database host and name are required")
    module = importlib.import_module("pymysql" if scheme == "mysql+pymysql" else "psycopg")
    args = {
        "host": parsed.hostname,
        "dbname" if scheme == "postgresql+psycopg" else "database": unquote(parsed.path.lstrip("/")),
        "autocommit": True,
    }
    if parsed.username is not None:
        args["user"] = unquote(parsed.username)
    if parsed.password is not None:
        args["password"] = unquote(parsed.password)
    if parsed.port is not None:
        args["port"] = parsed.port
    return Connection(module.connect(**args), "mysql" if scheme == "mysql+pymysql" else "postgresql")
