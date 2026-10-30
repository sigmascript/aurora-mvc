"""Opt-in async boundary for a complete synchronous database work unit.

A connection is opened, used, and closed on the same worker thread.
This is not an implicit async ORM and does not offload existing sync calls.
"""

import asyncio
import inspect

from .core import connect
from .models import Model


def _run_work(url, callback):
    with connect(url) as connection:
        with Model.connection_scope(connection):
            return callback(connection)


async def run_database_work(url, callback):
    """Run one synchronous callback in a worker with a private connection.

    The callback must finish all database operations before returning. Do not
    return the connection, a cursor, a lazy query, or a database-backed iterator.
    Use ``with connection.transaction():`` inside the callback when a unit of
    work requires atomicity. Never spawn child tasks that use its connection.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Database URL must be a nonempty string")
    if not callable(callback) or inspect.iscoroutinefunction(callback):
        raise TypeError("Database work must be a synchronous callable")
    return await asyncio.to_thread(_run_work, url, callback)
