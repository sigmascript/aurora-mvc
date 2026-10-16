"""Alpha release checks for ASGI lifespan and database shutdown boundaries."""

import asyncio
import unittest

from aurora.core.application import Application
from aurora.database.async_routed import RoutedWorkManager
from aurora.database.database_config import DatabaseConfiguration
from aurora.database.database_registry import DatabaseRegistry


class ASGILifecycleBoundaryTests(unittest.TestCase):
    def test_shutdown_without_prior_startup(self):
        async def scenario():
            app = Application()
            sent = []

            async def receive():
                return {"type": "lifespan.shutdown"}

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan"}, receive, send)
            return sent

        self.assertEqual(
            asyncio.run(scenario()),
            [{"type": "lifespan.shutdown.complete"}],
        )

    def test_startup_after_shutdown_is_rejected(self):
        async def scenario():
            app = Application()
            sent = []

            async def send(message):
                sent.append(message)

            async def shutdown_receive():
                return {"type": "lifespan.shutdown"}

            async def startup_receive():
                return {"type": "lifespan.startup"}

            await app({"type": "lifespan"}, shutdown_receive, send)
            await app({"type": "lifespan"}, startup_receive, send)
            return sent

        messages = asyncio.run(scenario())
        self.assertEqual(messages[0]["type"], "lifespan.shutdown.complete")
        self.assertEqual(messages[1]["type"], "lifespan.startup.failed")
        self.assertNotIn("traceback", messages[1]["message"].lower())

    def test_database_shutdown_closes_manager_and_registry(self):
        async def scenario():
            registry = DatabaseRegistry(
                DatabaseConfiguration({"default": {"url": "sqlite:///:memory:"}})
            )
            manager = RoutedWorkManager(None)
            app = Application(database_work=manager, database_registry=registry)
            sent = []

            async def receive():
                return {"type": "lifespan.shutdown"}

            async def send(message):
                sent.append(message)

            await app({"type": "lifespan"}, receive, send)
            with self.assertRaises(RuntimeError):
                await manager.run(str, lambda transaction: None)
            with self.assertRaises(RuntimeError):
                registry.get()
            return sent

        self.assertEqual(
            asyncio.run(scenario()),
            [{"type": "lifespan.shutdown.complete"}],
        )


if __name__ == "__main__":
    unittest.main()
