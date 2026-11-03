"""Pre-cutover AuroraSQL routing and transaction compatibility contracts."""

import asyncio

import pytest

from aurora.database.async_routed import RoutedWorkManager
from aurora.database.async_work import run_database_work
from aurora.database.configuration import DatabaseConfiguration


def _configuration(tmp_path):
    return DatabaseConfiguration({
        "default": {"url": f"sqlite:///{tmp_path / 'default.db'}"},
        "audit": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
    })


def test_transaction_commit_rollback_and_alias_isolation(tmp_path):
    async def scenario():
        async with RoutedWorkManager(_configuration(tmp_path)) as manager:
            for alias in ("default", "audit"):
                await manager.run(alias, lambda db: db.execute(
                    "CREATE TABLE events (id INTEGER PRIMARY KEY)"))

            await manager.run_transaction("default", lambda db: db.execute(
                "INSERT INTO events(id) VALUES (1)"))

            def fail(db):
                db.execute("INSERT INTO events(id) VALUES (2)")
                raise ValueError("rollback probe")

            with pytest.raises(ValueError, match="rollback probe"):
                await manager.run_transaction("default", fail)

            assert await manager.run("default", lambda db: db.execute(
                "SELECT id FROM events")) == [{"id": 1}]

            assert await manager.run("audit", lambda db: db.execute(
                "SELECT id FROM events")) == []

    asyncio.run(scenario())


def test_transaction_rejects_nested_transaction(tmp_path):
    async def scenario():
        async with RoutedWorkManager(_configuration(tmp_path)) as manager:
            def nested(db):
                with db.transaction():
                    pass

            with pytest.raises(RuntimeError, match="Nested transactions"):
                await manager.run_transaction("default", nested)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["connection", "query", "awaitable"])
def test_connection_bound_or_async_results_cannot_escape(tmp_path, kind):
    from aurora.database import Model, column

    class Entry(Model):
        id = column(int, primary_key=True)

    async def scenario():
        async with RoutedWorkManager(_configuration(tmp_path)) as manager:
            def callback(db):
                if kind == "connection":
                    return db

                if kind == "query":
                    return Entry.using(db)

                async def late():
                    return 1
                return late()

            with pytest.raises(TypeError, match="must not return"):
                await manager.run_transaction("default", callback)

    asyncio.run(scenario())


def test_unknown_alias_rejected_before_callback(tmp_path):
    async def scenario():
        async with RoutedWorkManager(_configuration(tmp_path)) as manager:
            invoked = []

            with pytest.raises(ValueError, match="Unknown database alias"):
                await manager.run_transaction("unknown", lambda db: invoked.append(True))

            assert invoked == []
    asyncio.run(scenario())


def test_direct_work_rejects_connection_escape(tmp_path):
    url = f"sqlite:///{tmp_path / 'direct.db'}"
    async def scenario():
        with pytest.raises(TypeError, match="connection-bound"):
            await run_database_work(url, lambda db: db)
    asyncio.run(scenario())
