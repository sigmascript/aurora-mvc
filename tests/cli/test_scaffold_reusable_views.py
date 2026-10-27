"""Phase 5.10: Jinja includes/macros and ownership of shared files."""
from pathlib import Path
import tempfile
import unittest

from aurora.cli import add_app, create_project
from aurora.rendering import ViewRenderer
from aurora.view import view


class TestReusableViews(unittest.TestCase):
    def test_mvc_macro_renders_and_escapes_arguments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('mvc_demo', directory=Path(temp), mvc=True)
            html = ViewRenderer(root / 'views').render(view('home', title='<Welcome>'))
            self.assertIn('&lt;Welcome&gt;', html)
            self.assertIn('class="action-link"', html)
            self.assertIn('https://auroramvc.com', html)
            self.assertTrue((root / 'views/components/actions.html').is_file())

    def test_modular_include_and_macro_render(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('mod_demo', directory=Path(temp), modular=True)
            renderer = ViewRenderer(root / 'views', view_namespaces={'blog': root / 'apps/blog/views'})
            html = renderer.render(view('blog/index', title='Blog'))
            self.assertIn('aria-label="Main navigation"', html)
            self.assertIn('href="/blog/"', html)
            self.assertIn('class="action-link"', html)

    def test_create_app_preserves_customized_shared_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('mod_demo', directory=Path(temp), modular=True)
            files = [root / 'views/layout.html',
                     root / 'views/navigation.html',
                     root / 'views/components.html',
                     root / 'statics/css/site.css']
            for idx, file in enumerate(files):
                file.write_bytes(f'custom content {idx}\n'.encode())
            before = {file: file.read_bytes() for file in files}
            add_app('news', directory=root)
            self.assertEqual(before, {file: file.read_bytes() for file in files})
            self.assertTrue((root / 'apps/news/views/index.html').is_file())

    def test_duplicate_app_does_not_touch_existing_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = create_project('mod_demo', directory=Path(temp), modular=True)
            add_app('news', directory=root)
            layout = root / 'views/layout.html'
            original = layout.read_bytes()
            with self.assertRaises(FileExistsError):
                add_app('news', directory=root)
            self.assertEqual(original, layout.read_bytes())
