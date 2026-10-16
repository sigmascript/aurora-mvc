from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Column as SQLAlchemyColumn,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)


TYPE_MAP = {
    str: String,
    int: Integer,
    float: Float,
    bool: Boolean,
    datetime: DateTime,
    date: Date,
}


class Column:
    """Declare a model column using a supported Python type.

    Supported types are str, int, float, bool, datetime, and date.
    Columns are non-nullable by default. Optional length applies only
    to str columns and must be a positive integer, excluding bool.
    """

    def __init__(
        self,
        python_type: type,
        *,
        primary_key=False,
        nullable=False,
        default=None,
        server_default=None,
        length=None,
        unique=False,
        references=None,
    ):
        if python_type not in TYPE_MAP:
            raise TypeError(f"Unsupported column type: {python_type.__name__}")

        if length is not None:
            if python_type is not str:
                raise TypeError("Column length is only supported for str columns")

            # bool is an int subclass, but is not a valid column length.
            if (
                not isinstance(length, int)
                or isinstance(length, bool)
                or length <= 0
            ):
                raise ValueError("Column length must be a positive integer")

        if not isinstance(unique, bool):
            raise TypeError("Column unique must be a boolean")

        if references is not None:
            if not isinstance(references, str) or not references.strip():
                raise TypeError("Column references must be a non-empty table.column string")
            if references.count(".") < 1 or any(not part for part in references.split(".")):
                raise ValueError("Column references must use table.column syntax")

        if server_default is not None and not isinstance(server_default, str):
            raise TypeError("server_default must be a literal string or None")

        self.server_default = server_default
        self.python_type = python_type
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.length = length
        self.unique = unique
        self.references = references

    def to_sqlalchemy(self, name: str) -> SQLAlchemyColumn:
        """Create a named SQLAlchemy column with the declared options."""
        type_class = TYPE_MAP[self.python_type]

        if self.python_type is str:
            sql_type = type_class(self.length)
        else:
            sql_type = type_class()

        constraints = [ForeignKey(self.references)] if self.references else []

        return SQLAlchemyColumn(
            name,
            sql_type,
            *constraints,
            primary_key=self.primary_key,
            unique=self.unique,
            nullable=self.nullable,
            default=self.default,
            server_default=self.server_default,
        )


def column(
    python_type: type,
    *,
    primary_key=False,
    nullable=False,
    default=None,
    server_default=None,
    length=None,
    unique=False,
    references=None,
) -> Column:
    """Create a Column, for example column(str, length=100)."""
    return Column(
        python_type,
        primary_key=primary_key,
        nullable=nullable,
        default=default,
        server_default=server_default,
        length=length,
        unique=unique,
        references=references,
    )
