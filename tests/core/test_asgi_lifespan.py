import asyncio
import unittest
from unittest.mock import Mock

from aurora import Aurora
from aurora.database.async_routed import RoutedWorkManager
from aurora.database.database_config import DatabaseConfiguration
from aurora.database.database_registry import DatabaseRegistry
from aurora.database.model_routing import ModelDatabaseRouting
from aurora.database.project_runtime import configure_project_databases


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
            Aurora(database_work=RoutedWorkManager(None), database_registry=object())
        registry.close()

    def test_project_config_rejects_bad_routing(self):
        with self.assertRaises(TypeError):
            configure_project_databases({'default': {'url': 'sqlite:///:memory:'}}, None)

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
            manager = RoutedWorkManager(None)
            app = Aurora(database_work=manager, database_registry=registry)
            events = iter([{'type': 'lifespan.startup'}, {'type': 'lifespan.shutdown'}])
            sent = []
            async def receive(): return next(events)
            async def send(message): sent.append(message)
            await app({'type': 'lifespan'}, receive, send)
            with self.assertRaises(RuntimeError): registry.get()
            with self.assertRaises(RuntimeError): await manager.run(str, lambda tx: 1)
            return sent
        self.assertEqual([x['type'] for x in asyncio.run(scenario())],
                         ['lifespan.startup.complete', 'lifespan.shutdown.complete'])

    def test_failed_drain_preserves_registry(self):
        async def scenario():
            registry = DatabaseRegistry(DatabaseConfiguration({'default': {'url': 'sqlite:///:memory:'}}))
            manager = RoutedWorkManager(None)
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
