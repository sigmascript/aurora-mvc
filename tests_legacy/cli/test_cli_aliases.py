import ast
import tempfile
import unittest
from pathlib import Path
from aurora.cli import create_project, create_controller, _controller_binding


class ConditionalAliasTests(unittest.TestCase):
    def test_plain_and_conflicting_controller_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project('demo', modular=True, directory=Path(tmp))
            create_controller('blog', 'News', directory=root)
            create_controller('home', 'News', directory=root)
            source = (root / 'app.py').read_text(encoding='utf-8')
            self.assertIn('from apps.blog.controllers.News import News\n', source)
            self.assertIn('from apps.home.controllers.News import News as HomeNews\n', source)
            self.assertIn('app.route("/blog/news/", News)', source)
            self.assertIn('app.route("/home/news/", HomeNews)', source)
            ast.parse(source)

    def test_alias_collision_uses_suffix(self):
        source = 'from x import News\nHomeNews = object()\n'
        self.assertEqual(_controller_binding(source, 'home', 'News'), 'HomeNews2')

    def test_other_bindings_count(self):
        source = 'class News: pass\n'
        self.assertEqual(_controller_binding(source, 'blog', 'News'), 'BlogNews')

    def test_wildcard_rejected(self):
        with self.assertRaises(ValueError):
            _controller_binding('from x import *\n', 'blog', 'News')

    def test_generated_module_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project('demo', modular=True, directory=Path(tmp))
            destination = create_controller('blog', 'Article', directory=root)
            ast.parse(destination.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
