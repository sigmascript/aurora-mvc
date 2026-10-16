"""Read-only SQLite schema inspection (no migration operations)."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    default: str | None
    primary_key_position: int


@dataclass(frozen=True)
class IndexInfo:
    name: str
    unique: bool
    columns: tuple[str, ...]
    origin: str
    partial: bool


@dataclass(frozen=True)
class ForeignKeyInfo:
    id: int
    sequence: int
    referenced_table: str
    from_column: str
    to_column: str | None
    on_update: str
    on_delete: str


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    indexes: tuple[IndexInfo, ...]
    foreign_keys: tuple[ForeignKeyInfo, ...]


@dataclass(frozen=True)
class SchemaInfo:
    tables: tuple[TableInfo, ...]


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def inspect_sqlite(path: str | Path) -> SchemaInfo:
    """Inspect an existing SQLite file with SQLite's read-only connection mode.

    Missing paths and in-memory databases are deliberately unsupported.
    This function never creates or alters a database, including journal files.
    """
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('Expected an existing SQLite database file')
    uri = path.as_uri() + '?mode=ro&immutable=1'
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute('PRAGMA query_only=ON')
        tables = []
        names = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (name,) in names:
            quoted = _quote_identifier(name)
            columns = tuple(ColumnInfo(row[1], row[2], not bool(row[3]),
                                       row[4], row[5]) for row in connection.execute(
                f'PRAGMA table_info({quoted})'))
            indexes = []
            for idx in connection.execute(f'PRAGMA index_list({quoted})').fetchall():
                index_name, unique, origin, partial = idx[1], idx[2], idx[3], idx[4]
                index_columns = tuple(row[2] for row in connection.execute(
                    f'PRAGMA index_info({_quote_identifier(index_name)})'))
                indexes.append(IndexInfo(index_name, bool(unique), index_columns,
                                         origin, bool(partial)))
            foreign_keys = tuple(ForeignKeyInfo(row[0], row[1], row[2], row[3],
                                               row[4], row[5], row[6])
                                 for row in connection.execute(f'PRAGMA foreign_key_list({quoted})'))
            tables.append(TableInfo(name, columns, tuple(indexes), foreign_keys))
        return SchemaInfo(tuple(tables))
