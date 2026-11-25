"""Opt-in async boundary for complete, thread-owned AuroraSQL work units.

Each callback runs with a private connection and scoped Model binding. A
transaction is explicit unless the caller selects transactional execution.
"""

import asyncio
import inspect

from .configuration import DatabaseConfiguration
from .connections import Connection, connect
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


class RoutedWorkManager:
    def __init__(self, configuration: DatabaseConfiguration, *, max_concurrent=8):
        if (
            isinstance(max_concurrent, bool)
            or not isinstance(max_concurrent, int)
            or max_concurrent < 1
        ):
            raise ValueError("max_concurrent must be a positive integer")

        if not isinstance(configuration, DatabaseConfiguration):
            raise TypeError("configuration must be a DatabaseConfiguration")

        self._configuration = configuration
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._pending = set()
        self._closed = False
        self._loop = None

    @property
    def pending_count(self):
        return len(self._pending)

    def _check_loop(self):
        loop = asyncio.get_running_loop()

        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("RoutedWorkManager cannot be shared across event loops")

    async def run(self, alias, callback):
        self._check_loop()

        if self._closed:
            raise RuntimeError("RoutedWorkManager is closed")

        _validate_callback(callback)
        url = self._configuration.resolve(alias)
        task = asyncio.create_task(self._execute(url, callback, transactional=False))
        self._pending.add(task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

    async def run_transaction(self, alias, callback):
        """Run a complete callback in an owned, atomic transaction.

        The callback receives a scoped AuroraSQL connection, not a legacy
        SQLAlchemy transaction. Cancellation does not interrupt committed work.
        """
        self._check_loop()

        if self._closed:
            raise RuntimeError("RoutedWorkManager is closed")

        _validate_callback(callback)
        url = self._configuration.resolve(alias)
        task = asyncio.create_task(self._execute(url, callback, transactional=True))
        self._pending.add(task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

    async def _execute(self, url, callback, transactional):
        async with self._semaphore:
            return await asyncio.to_thread(_run_work, url, callback, transactional)

    def _completed(self, task):
        self._pending.discard(task)

        if not task.cancelled():
            task.exception()

    async def aclose(self):
        self._check_loop()
        self._closed = True

        while self._pending:
            await asyncio.shield(asyncio.gather(*tuple(self._pending), return_exceptions=True))

    async def __aenter__(self):
        self._check_loop()

        if self._closed:
            raise RuntimeError("RoutedWorkManager is closed")

        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()
