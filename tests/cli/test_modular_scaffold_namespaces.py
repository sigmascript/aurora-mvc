"""Phase 5.11: compact app views, real macros, and backward compatibility."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from aurora.cli import create_project, add_app
from aurora.rendering import ViewRenderer
from aurora.view import View


class ModularScaffoldNamespaceTests(unittest.TestCase):
    def test_namespaced_templates_do_not_collide(self):
        with TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp), modular=True)
            renderer = ViewRenderer(root / 'views', view_namespaces={
                'home': root / 'apps/home/views',
                'blog': root / 'apps/blog/views',
            })
            home = renderer.render(View('home/index', title='Home'))
            blog = renderer.render(View('blog/index', title='Blog'))
            self.assertIn('modular Aurora project', home)
            self.assertIn('blog app', blog)
            self.assertNotEqual(home, blog)
            self.assertFalse((root / 'apps/blog/views/blog').exists())

    def test_created_app_uses_shared_macro(self):
        with TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp), modular=True)
            add_app('news', directory=root)
            source = (root / 'apps/news/views/index.html').read_text()
            self.assertIn('import action_link', source)
            self.assertIn('action_link("Back to home", "/")', source)
            renderer = ViewRenderer(root / 'views', view_namespaces={'news': root / 'apps/news/views'})
            html = renderer.render(View('news/index', title='News'))
            self.assertIn('class="action-link"', html)
            self.assertIn('Back to home', html)

    def test_app_creation_preserves_shared_files(self):
        with TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp), modular=True)
            paths = [root / 'views/layout.html', root / 'views/components.html',
                     root / 'statics/css/site.css']
            for path in paths:
                path.write_bytes(b'custom developer content')
            add_app('news', directory=root)
            self.assertTrue(all(p.read_bytes() == b'custom developer content' for p in paths))

    def test_old_view_directory_loading_remains_supported(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'legacy.html').write_text('<h1>{{ title }}</h1>')
            self.assertEqual(ViewRenderer(root).render(View('legacy', title='Legacy')), '<h1>Legacy</h1>')
