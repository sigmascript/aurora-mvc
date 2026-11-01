import asyncio
import unittest
from unittest.mock import Mock

from aurora import Aurora
from aurora.database.async_routed import RoutedWorkManager
from aurora.database.configuration import DatabaseConfiguration
from aurora.database.database_registry import DatabaseRegistry


class ASGILifespanTests(unittest.TestCase):
    def test_legacy_application(self):
        self.assertIsNone(Aurora().database_work)

    def test_pair_required(self):
        with self.assertRaises(ValueError):
            Aurora(database_work=object())
        with self.assertRaises(ValueError):
            Aurora(database_registry=object())

    def test_type_checks(self):
        registry = DatabaseRegistry(DatabaseConfiguration({'default': {'url': 'sqlite:///:memory:'}}))
        with self.assertRaises(TypeError):
            Aurora(database_work=object(), database_registry=registry)
        with self.assertRaises(TypeError):
            Aurora(database_work=RoutedWorkManager(DatabaseConfiguration({"default": {"url": "sqlite:///:memory:"}})), database_registry=object())
        registry.close()

    def test_lifespan_no_database(self):
        async def scenario():
            app = Aurora()
            events = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
            sent = []
            async def receive(): return next(events)
            async def send(message): sent.append(message)
            await app({'type': 'lifespan'}, receive, send)
            return sent
        self.assertEqual([x['type'] for x in asyncio.run(scenario())],
                         ['lifespan.startup.complete', 'lifespan.shutdown.complete'])

    def test_lifespan_closes_registry_after_drain(self):
        async def scenario():
            registry = DatabaseRegistry(DatabaseConfiguration({'default': {'url': 'sqlite:///:memory:'}}))
            manager = RoutedWorkManager(DatabaseConfiguration({"default": {"url": "sqlite:///:memory:"}}))
            app = Aurora(database_work=manager, database_registry=registry)
            events = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
            sent = []
            async def receive(): return next(events)
            async def send(message): sent.append(message)
            await app({'type': 'lifespan'}, receive, send)
            with self.assertRaises(RuntimeError): registry.get()
            with self.assertRaises(RuntimeError): await manager.run("default", lambda connection: 1)
            return sent
        self.assertEqual([x['type'] for x in asyncio.run(scenario())],
                         ['lifespan.startup.complete', 'lifespan.shutdown.complete'])

    def test_failed_drain_preserves_registry(self):
        async def scenario():
            registry = DatabaseRegistry(DatabaseConfiguration({'default': {'url': 'sqlite:///:memory:'}}))
            manager = RoutedWorkManager(DatabaseConfiguration({"default": {"url": "sqlite:///:memory:"}}))
            async def fail(): raise RuntimeError('drain failed')
            manager.aclose = fail
            app = Aurora(database_work=manager, database_registry=registry)
            async def receive(): return {'type': 'lifespan.shutdown'}
            sent = []
            async def send(message): sent.append(message)
            await app({'type': 'lifespan'}, receive, send)
            db = registry.get()
            registry.close()
            return sent, db
        sent, _ = asyncio.run(scenario())
        self.assertEqual(sent[0]['type'], 'lifespan.shutdown.failed')
        self.assertNotIn('drain failed', sent[0]['message'])

if __name__ == '__main__': unittest.main()
