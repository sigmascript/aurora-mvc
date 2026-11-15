"""AuroraSQL lifecycle coverage before the canonical database package cutover."""

import asyncio

import pytest

from aurora.database.configuration import DatabaseConfiguration
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.async_routed import RoutedWorkManager


def config(tmp_path):
    return DatabaseConfiguration({"default": {"url": f"sqlite:///{tmp_path / 'main.db'}"}, "audit": {"url": f"sqlite:///{tmp_path / 'audit.db'}"}})


def test_registry_lazy_aliases_and_close(tmp_path):
    registry = DatabaseRegistry(config(tmp_path))
    assert registry.aliases() == ("default", "audit")

    assert registry.initialized_aliases() == ()

    connection = registry.get("audit")
    assert registry.get("audit") is connection

    assert registry.initialized_aliases() == ("audit",)

    registry.close()
    registry.close()

    with pytest.raises(RuntimeError, match="closed"):
        registry.get()


def test_registry_rejects_unknown_alias(tmp_path):
    registry = DatabaseRegistry(config(tmp_path))

    with pytest.raises(ValueError, match="Unknown database alias"):
        registry.get("missing")

    assert registry.initialized_aliases() == ()

    registry.close()


def test_work_manager_isolated_aliases_and_drains(tmp_path):
    async def scenario():
        manager = RoutedWorkManager(config(tmp_path), max_concurrent=2)

        async with manager:
            assert await manager.run("default", lambda db: db.execute("SELECT 10")[0]["10"]) == 10

            assert await manager.run("audit", lambda db: db.execute("SELECT 20")[0]["20"]) == 20

        assert manager.pending_count == 0

        with pytest.raises(RuntimeError, match="closed"):
            await manager.run("default", lambda db: 1)
    asyncio.run(scenario())


def test_work_manager_rejects_async_callbacks(tmp_path):
    async def callback(db):
        return 1
    async def scenario():
        manager = RoutedWorkManager(config(tmp_path))

        with pytest.raises(TypeError, match="synchronous"):
            await manager.run("default", callback)

        await manager.aclose()
    asyncio.run(scenario())


def test_work_manager_rejects_invalid_capacity(tmp_path):
    for value in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            RoutedWorkManager(config(tmp_path), max_concurrent=value)
