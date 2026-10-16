import unittest
from aurora.database import Model, column
from aurora.database.registry import ModelRegistry


class ModelRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class RegistryShared6102(Model):
            id = column(int, primary_key=True)
        class RegistryBlog6102(Model):
            id = column(int, primary_key=True)
        class RegistryAnalytics6102(Model):
            id = column(int, primary_key=True)
        cls.shared = RegistryShared6102
        cls.blog = RegistryBlog6102
        cls.analytics = RegistryAnalytics6102

    def test_explicit_empty(self):
        self.assertEqual(len(ModelRegistry()), 0)

    def test_shared_registration(self):
        entry = ModelRegistry().register(self.shared)
        self.assertEqual(entry.qualified_name, 'RegistryShared6102')

    def test_app_registration(self):
        entry = ModelRegistry().register(self.blog, app='blog')
        self.assertEqual(entry.qualified_name, 'blog.RegistryBlog6102')

    def test_idempotent(self):
        r = ModelRegistry()
        self.assertIs(r.register(self.shared), r.register(self.shared))
        self.assertEqual(len(r), 1)

    def test_conflicting_ownership(self):
        r = ModelRegistry()
        r.register(self.blog, app='blog')
        with self.assertRaises(ValueError):
            r.register(self.blog, app='other')

    def test_lookup(self):
        r = ModelRegistry()
        r.register(self.blog, app='blog')
        self.assertEqual(r.get('RegistryBlog6102').model, self.blog)
        self.assertEqual(r.get('blog.RegistryBlog6102').model, self.blog)

    def test_unknown_lookup(self):
        with self.assertRaises(KeyError):
            ModelRegistry().get('Unknown')

    def test_list_filter(self):
        r = ModelRegistry()
        r.register(self.shared)
        r.register(self.blog, app='blog')
        self.assertEqual(len(r.list()), 2)
        self.assertEqual(len(r.list(app='blog')), 1)

    def test_invalid_model(self):
        with self.assertRaises(TypeError):
            ModelRegistry().register(object)

    def test_invalid_app(self):
        with self.assertRaises(ValueError):
            ModelRegistry().register(self.blog, app='bad-app')
