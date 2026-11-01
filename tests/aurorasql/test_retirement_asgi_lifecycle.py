"""Pre-cutover ASGI lifecycle acceptance for the AuroraSQL integration pair."""

import asyncio

import pytest

from aurora import Aurora
from aurora.aurorasql.async_routed import RoutedWorkManager
from aurora.aurorasql.configuration import DatabaseConfiguration
from aurora.aurorasql.database_registry import DatabaseRegistry
from aurora.database.async_routed import RoutedWorkManager as LegacyWorkManager
from aurora.database.database_config import DatabaseConfiguration as LegacyConfiguration
from aurora.database.database_registry import DatabaseRegistry as LegacyRegistry


def components(tmp_path):
    config = DatabaseConfiguration({"default": {"url": f"sqlite:///{tmp_path / 'app.sqlite3'}"}})
    return RoutedWorkManager(config), DatabaseRegistry(config)


async def lifespan(app, *types):
    events = iter({"type": kind} for kind in types)
    sent = []

    async def receive():
        return next(events)

    async def send(message):
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)
    return sent


def test_modern_pair_accepted_without_opening_connections(tmp_path):
    manager, registry = components(tmp_path)
    app = Aurora(database_work=manager, database_registry=registry)
    assert registry.initialized_aliases() == ()
    assert app.database_work is manager
    assert app.database_registry is registry


def test_mixed_legacy_and_modern_pairs_rejected(tmp_path):
    modern_manager, modern_registry = components(tmp_path)
    legacy_config = LegacyConfiguration({"default": {"url": "sqlite:///:memory:"}})
    legacy_registry = LegacyRegistry(legacy_config)
    legacy_manager = LegacyWorkManager(None)
    try:
        with pytest.raises(TypeError, match="same database backend"):
            Aurora(database_work=modern_manager, database_registry=legacy_registry)
        with pytest.raises(TypeError, match="same database backend"):
            Aurora(database_work=legacy_manager, database_registry=modern_registry)
    finally:
        legacy_registry.close()
        modern_registry.close()


def test_modern_shutdown_drains_then_closes(tmp_path):
    async def scenario():
        manager, registry = components(tmp_path)
        app = Aurora(database_work=manager, database_registry=registry)
        assert await manager.run("default", lambda connection: 7) == 7
        sent = await lifespan(app, "lifespan.startup", "lifespan.shutdown")
        assert [item["type"] for item in sent] == [
            "lifespan.startup.complete", "lifespan.shutdown.complete"
        ]
        with pytest.raises(RuntimeError, match="closed"):
            registry.get()
        with pytest.raises(RuntimeError, match="closed"):
            await manager.run("default", lambda connection: None)

    asyncio.run(scenario())


def test_failed_modern_drain_does_not_close_registry(tmp_path):
    async def scenario():
        manager, registry = components(tmp_path)
        async def failing_drain():
            raise RuntimeError("secret database details")
        manager.aclose = failing_drain
        app = Aurora(database_work=manager, database_registry=registry)
        sent = await lifespan(app, "lifespan.shutdown")
        assert sent == [{"type": "lifespan.shutdown.failed", "message": "Database shutdown failed"}]
        assert registry.get() is not None
        registry.close()

    asyncio.run(scenario())
