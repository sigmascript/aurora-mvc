"""Bounded, draining async lifecycle for AuroraSQL database work.

Each accepted job opens and closes its own connection inside a worker thread.
Cancellation of a waiter does not interrupt an already accepted job.
"""

import asyncio

from .async_work import _run_work, _validate_callback
from .configuration import DatabaseConfiguration


class RoutedWorkManager:
    def __init__(self, configuration: DatabaseConfiguration, *, max_concurrent=8):
        if isinstance(max_concurrent, bool) or not isinstance(max_concurrent, int) or max_concurrent < 1:
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
