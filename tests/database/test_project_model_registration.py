import unittest

from aurora.database import Model, column
from aurora.database.model_registration import register_project_models
from aurora.database.registry import ModelRegistry


class ModelRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Shared6102B(Model):
            id = column(int, primary_key=True)
        class Blog6102B(Model):
            id = column(int, primary_key=True)
        cls.shared = Shared6102B
        cls.blog = Blog6102B

    def test_empty(self):
        self.assertEqual(len(register_project_models()), 0)

    def test_shared(self):
        self.assertIs(register_project_models(shared=[self.shared]).get('Shared6102B').model, self.shared)

    def test_app(self):
        self.assertIs(register_project_models(apps={'blog': [self.blog]}).get('blog.Blog6102B').model, self.blog)

    def test_mixed(self):
        registry = register_project_models(shared=[self.shared], apps={'blog': [self.blog]})
        self.assertEqual(len(registry), 2)

    def test_explicit_registry(self):
        registry = ModelRegistry()
        self.assertIs(register_project_models(shared=[self.shared], registry=registry), registry)

    def test_repeat_is_idempotent(self):
        registry = register_project_models(shared=[self.shared])
        register_project_models(shared=[self.shared], registry=registry)
        self.assertEqual(len(registry), 1)

    def test_conflict_is_atomic(self):
        registry = register_project_models(shared=[self.shared])
        with self.assertRaises(ValueError):
            register_project_models(apps={'blog': [self.blog, self.shared]}, registry=registry)
        self.assertEqual(len(registry), 1)
        self.assertEqual(registry.get('Shared6102B').app, None)

    def test_invalid_app_is_atomic(self):
        registry = ModelRegistry()
        with self.assertRaises(ValueError):
            register_project_models(shared=[self.shared], apps={'bad-name': [self.blog]}, registry=registry)
        self.assertEqual(len(registry), 0)

    def test_invalid_model_is_atomic(self):
        registry = ModelRegistry()
        with self.assertRaises(TypeError):
            register_project_models(shared=[self.shared, object], registry=registry)
        self.assertEqual(len(registry), 0)

    def test_does_not_bind_database(self):
        original = Model.database
        register_project_models(shared=[self.shared], apps={'blog': [self.blog]})
        self.assertIs(Model.database, original)
