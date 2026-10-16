"""Phase 5.11.1 generated-code and preservation contract."""
import tempfile
import unittest
from pathlib import Path
from aurora.cli import create_project, add_app
from aurora.rendering import ViewRenderer
from aurora.view import View


class CleanModularScaffoldTests(unittest.TestCase):
    def test_concrete_blog_controller_and_flat_shared_views(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp), modular=True)
            source = (root / 'app.py').read_text()
            self.assertIn('from apps.blog.controllers.blog import Blog', source)
            self.assertIn('app.route("/blog", Blog)', source)
            self.assertNotIn('BlogIndex', source)
            self.assertIn('class Blog(Controller):', (root / 'apps/blog/controllers/blog.py').read_text())
            self.assertFalse((root / 'apps/blog/controllers/index.py').exists())
            self.assertFalse((root / 'views/shared').exists())
            for name in ('layout.html', 'navigation.html', 'components.html'):
                self.assertTrue((root / 'views' / name).is_file())

    def test_templates_render_macros_and_inheritance(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp), modular=True)
            news = add_app('news', directory=root)
            renderer = ViewRenderer(root / 'views', view_namespaces={
                'home': root / 'apps/home/views',
                'blog': root / 'apps/blog/views',
                'news': news / 'views',
            })
            for name in ('home', 'blog', 'news'):
                html = renderer.render(View(f'{name}/index', title=name))
                self.assertIn('<html', html)
                self.assertIn('Main navigation', html)
            self.assertIn('Back to home', renderer.render(View('news/index', title='News')))

    def test_create_app_never_overwrites_shared_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('demo', directory=Path(temp), modular=True)
            files = [root / 'views' / name for name in ('layout.html', 'navigation.html', 'components.html')]
            files += [root / 'statics/css/site.css']
            for i, file in enumerate(files):
                file.write_bytes(f'custom-{i}'.encode())
            add_app('news', directory=root)
            self.assertEqual([f.read_bytes() for f in files], [f'custom-{i}'.encode() for i in range(len(files))])
