"""Offline preparation of restricted nullable MySQL ADD COLUMN proposals.

Proposals are review artifacts only: no approval, connection, or DDL execution.
The CREATE_TABLE operation contract deliberately remains unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import re

from sqlalchemy.dialects import mysql
from sqlalchemy.sql.sqltypes import NullType

from aurora.database.model import Model

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MODEL_SEGMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MySQLAddColumnProposal:
    database_alias: str
    schema_name: str
    table_name: str
    model_reference: str
    column_name: str

    def __post_init__(self) -> None:
        for field in ("database_alias", "schema_name", "table_name", "column_name"):
            value = getattr(self, field)
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"Invalid {field}")
        if not isinstance(self.model_reference, str) or self.model_reference.count(":") != 1:
            raise ValueError("Expected MODULE:CLASS model reference")
        module, cls = self.model_reference.split(":")
        if not module or not cls or not all(_MODEL_SEGMENT.fullmatch(s) for s in (*module.split("."), cls)):
            raise ValueError("Invalid model reference")


def prepare_mysql_nullable_add_column(proposal: MySQLAddColumnProposal) -> tuple[str, str]:
    """Compile exactly one safe nullable column and return (SQL, SHA256).

    The digest is a comparison aid, NOT permission to execute the SQL.
    A future executor must independently validate live schema and history.
    """
    if not isinstance(proposal, MySQLAddColumnProposal):
        raise TypeError("Structured MySQL ADD COLUMN proposal required")
    module, cls_name = proposal.model_reference.split(":")
    cls = getattr(importlib.import_module(module), cls_name, None)
    if not isinstance(cls, type) or not issubclass(cls, Model) or cls is Model:
        raise ValueError("Reference does not identify an Aurora model")
    table = cls.__sql_table__
    if table.schema is not None or table.name != proposal.table_name:
        raise ValueError("Model table does not match proposal")
    if proposal.column_name not in table.c:
        raise ValueError("Column does not exist in model")
    column = table.c[proposal.column_name]
    if (not column.nullable or column.primary_key or column.unique or column.index
            or column.foreign_keys or column.computed is not None
            or column.identity is not None or column.default is not None
            or column.server_default is not None or column.server_onupdate is not None
            or column.autoincrement is True):
        raise ValueError("Only plain nullable columns without defaults or constraints are supported")
    if isinstance(column.type, NullType):
        raise ValueError("Unresolved column type")
    dialect = mysql.dialect()
    type_sql = column.type.compile(dialect=dialect)
    if not type_sql or ";" in type_sql or "--" in type_sql or "/*" in type_sql:
        raise ValueError("Unsupported SQL column type")
    sql = f"ALTER TABLE `{proposal.table_name}` ADD COLUMN `{proposal.column_name}` {type_sql} NULL"
    return sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()
