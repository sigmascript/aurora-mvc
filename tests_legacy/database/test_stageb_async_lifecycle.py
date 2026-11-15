"""Stage B acceptance for cancellation, draining, and bounded worker ownership."""

import asyncio
import threading

import pytest

from aurora.database.async_routed import RoutedWorkManager
from aurora.database.configuration import DatabaseConfiguration


def _configuration(tmp_path):
    return DatabaseConfiguration({
        "default": {"url": f"sqlite:///{tmp_path / 'stageb.db'}"},
    })


def test_cancelled_shutdown_can_be_resumed_and_drains_owned_work(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def work(connection):
        observed.append(connection)
        entered.set()

        if not release.wait(5):
            raise RuntimeError("Worker was not released")

        return connection.execute("SELECT 1 AS value")[0]["value"]

    async def scenario():
        manager = RoutedWorkManager(_configuration(tmp_path), max_concurrent=1)
        waiter = asyncio.create_task(manager.run("default", work))
        assert await asyncio.to_thread(entered.wait, 3)

        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        closing.cancel()

        with pytest.raises(asyncio.CancelledError):
            await closing

        assert manager.pending_count == 1

        with pytest.raises(RuntimeError, match="closed"):
            await manager.run("default", work)

        release.set()
        assert await waiter == 1
        await manager.aclose()
        assert manager.pending_count == 0
        assert observed[0]._closed

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_cancelled_waiter_does_not_cancel_transaction(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    observed = []

    def work(connection):
        observed.append(connection)
        connection.execute_write("CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY)")
        entered.set()

        if not release.wait(5):
            raise RuntimeError("Worker was not released")

        connection.execute_write("INSERT INTO receipts (id) VALUES (1)")

    async def scenario():
        manager = RoutedWorkManager(_configuration(tmp_path))
        waiter = asyncio.create_task(manager.run_transaction("default", work))
        assert await asyncio.to_thread(entered.wait, 3)
        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert manager.pending_count == 1
        release.set()
        await manager.aclose()
        assert observed[0]._closed

        async with RoutedWorkManager(_configuration(tmp_path)) as verify:
            count = await verify.run(
                "default", lambda connection: connection.fetch_first(
                    "SELECT COUNT(*) AS total FROM receipts"
                )["total"]
            )
            assert count == 1

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_bounded_concurrency_and_shutdown_drains_queued_work(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    active = 0
    peak = 0

    def work(connection):
        nonlocal active, peak

        with lock:
            active += 1
            peak = max(peak, active)
            entered.set()

        try:
            if not release.wait(5):
                raise RuntimeError("Worker was not released")

            return connection.execute("SELECT 1 AS value")[0]["value"]
        finally:
            with lock:
                active -= 1

    async def scenario():
        manager = RoutedWorkManager(_configuration(tmp_path), max_concurrent=1)
        tasks = [asyncio.create_task(manager.run("default", work)) for _ in range(3)]
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.sleep(0)
        assert peak == 1
        assert manager.pending_count == 3

        closing = asyncio.create_task(manager.aclose())
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(*tasks) == [1, 1, 1]
        await closing
        assert manager.pending_count == 0
        assert peak == 1

    try:
        asyncio.run(scenario())
    finally:
        release.set()
