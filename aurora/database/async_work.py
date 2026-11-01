"""Opt-in async boundary for complete, thread-owned AuroraSQL work units.

Each callback runs with a private connection and scoped Model binding. A
transaction is explicit unless the caller selects transactional execution.
"""

import asyncio
import inspect

from .core import Connection, connect
from .models import Model, ModelOperations, Query


def _validate_callback(callback):
    if not callable(callback) or inspect.iscoroutinefunction(callback):
        raise TypeError("Database work must be a synchronous callable")


def _reject_escaping_handle(result, connection):
    """Reject known live database handles before the worker closes them."""
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()
        raise TypeError("Database callback must not return awaitable work")
    if isinstance(result, (Connection, ModelOperations, Query)) or result is connection:
        raise TypeError("Database callback must not return a connection-bound handle")
    if getattr(result, "connection", None) is connection:
        raise TypeError("Database callback must not return a connection-bound handle")
    return result


def _run_work(url, callback, transactional=False):
    with connect(url) as connection:
        with Model.connection_scope(connection):
            if transactional:
                with connection.transaction():
                    return _reject_escaping_handle(callback(connection), connection)
            return _reject_escaping_handle(callback(connection), connection)


async def run_database_work(url, callback):
    """Run one synchronous callback in a worker with a private connection.

    The callback must finish database operations before returning. Use
    ``with connection.transaction():`` for an atomic unit of work.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Database URL must be a nonempty string")
    _validate_callback(callback)
    return await asyncio.to_thread(_run_work, url, callback)
