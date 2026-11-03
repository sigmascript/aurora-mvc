"""Behavioral retirement gates for AuroraSQL lifecycle and isolation."""

import asyncio
import threading

import pytest

from aurora.database.async_routed import RoutedWorkManager
from aurora.database.configuration import DatabaseConfiguration
from aurora.database.database_registry import DatabaseRegistry


def configuration(tmp_path):
    return DatabaseConfiguration({
        "default": {"url": f"sqlite:///{tmp_path / 'default.db'}"},
        "audit": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
    })


def test_registry_connections_are_alias_isolated(tmp_path):
    registry = DatabaseRegistry(configuration(tmp_path))

    with registry:
        primary = registry.get("default")
        audit = registry.get("audit")
        assert primary is not audit

        primary.execute("CREATE TABLE records (id INTEGER PRIMARY KEY)")
        primary.execute("INSERT INTO records (id) VALUES (1)")
        assert primary.execute("SELECT id FROM records")[0]["id"] == 1

        with pytest.raises(Exception):
            audit.execute("SELECT id FROM records")

    with pytest.raises(RuntimeError, match="closed"):
        registry.get("default")


def test_work_manager_drains_cancelled_waiter(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def work(connection):
        entered.set()
        assert release.wait(5)

        return connection.execute("SELECT 42 AS answer")[0]["answer"]

    async def scenario():
        manager = RoutedWorkManager(configuration(tmp_path), max_concurrent=1)
        waiter = asyncio.create_task(manager.run("default", work))
        await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), 6)
        assert entered.is_set()

        waiter.cancel()

        with pytest.raises(asyncio.CancelledError):
            await waiter

        assert manager.pending_count == 1

        release.set()
        await manager.aclose()
        assert manager.pending_count == 0

        with pytest.raises(RuntimeError, match="closed"):
            await manager.run("default", work)

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_work_manager_failure_does_not_poison_following_work(tmp_path):
    def fail(connection):
        raise ValueError("expected callback failure")

    async def scenario():
        async with RoutedWorkManager(configuration(tmp_path)) as manager:
            with pytest.raises(ValueError, match="expected callback failure"):
                await manager.run("default", fail)

            result = await manager.run("default", lambda db: db.execute("SELECT 7 AS n")[0]["n"])
            assert result == 7

        assert manager.pending_count == 0

    asyncio.run(scenario())


def test_work_manager_rejects_unknown_alias_without_work(tmp_path):
    async def scenario():
        async with RoutedWorkManager(configuration(tmp_path)) as manager:
            with pytest.raises(ValueError, match="Unknown database alias"):
                await manager.run("missing", lambda db: None)

            assert manager.pending_count == 0

    asyncio.run(scenario())
