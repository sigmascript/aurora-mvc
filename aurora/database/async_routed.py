"""Async bridge for explicitly routed synchronous database work.

The entire transaction, including connection checkout and cleanup, executes
inside one worker thread. Transaction handles must never escape the callback.
"""
import asyncio
import inspect
from collections.abc import Callable

from aurora.database.routed_transactions import routed_transaction


def _run_sync(access, model, callback):
    with routed_transaction(access, model) as transaction:
        result = callback(transaction)
        if inspect.isawaitable(result):
            # Do not allow a coroutine to escape an already-closed transaction.
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("callback must be synchronous and return a non-awaitable result")
        if result is transaction or getattr(result, "_transaction", None) is transaction:
            raise ValueError("Transaction-bound objects cannot escape the callback")
        return result


async def run_routed_transaction(access, model, callback: Callable):
    """Run synchronous model work off the ASGI event loop.

    Callback receives one transaction and runs entirely in a worker thread.
    A new connection is acquired per invocation; no thread-global bindings.
    Cancellation of the awaiting task does NOT cancel a running worker or its
    transaction; callbacks must be bounded and side effects idempotent.
    """
    if not callable(callback):
        raise TypeError("callback must be callable")
    if inspect.iscoroutinefunction(callback):
        raise TypeError("callback must be synchronous")
    return await asyncio.to_thread(_run_sync, access, model, callback)


class RoutedWorkManager:
    """Async lifetime boundary for explicitly routed synchronous transactions.

    Create and use on one event loop. ``aclose`` drains accepted operations
    before callers dispose their DatabaseRegistry. The manager does not own
    or dispose the database registry.

    Cancellation of a waiting caller does not cancel its accepted worker.
    Work may commit after cancellation, so writes must be idempotent.
    """

    def __init__(self, access, *, max_concurrent: int = 8):
        if (
            not isinstance(max_concurrent, int)
            or isinstance(max_concurrent, bool)
            or max_concurrent < 1
        ):
            raise ValueError("max_concurrent must be a positive integer")
        self._access = access
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._pending = set()
        self._closed = False
        self._loop = None

    def _check_loop(self):
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("RoutedWorkManager cannot be shared across event loops")

    @property
    def pending_count(self):
        return len(self._pending)

    async def run(self, model, callback):
        """Submit bounded work; do not return transaction-bound handles."""
        self._check_loop()
        if self._closed:
            raise RuntimeError("RoutedWorkManager is closed")
        if not callable(callback) or inspect.iscoroutinefunction(callback):
            raise TypeError("callback must be a synchronous callable")
        # The task is registered before the first await, so shutdown sees it.
        task = asyncio.create_task(self._execute(model, callback))
        self._pending.add(task)
        task.add_done_callback(self._completed)
        return await asyncio.shield(task)

    async def _execute(self, model, callback):
        async with self._semaphore:
            return await asyncio.to_thread(_run_sync, self._access, model, callback)

    def _completed(self, task):
        self._pending.discard(task)
        # Retrieve errors even if the original waiter was cancelled.
        if not task.cancelled():
            task.exception()

    async def aclose(self):
        """Reject new work and wait for all accepted workers to finish.

        Even if shutdown is cancelled, already accepted workers keep running;
        call aclose again before disposing engines.
        """
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
