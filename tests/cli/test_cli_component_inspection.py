"""Read-only grouped CLI component inspection tests."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aurora.cli import create_project, main, list_components, show_component

class ComponentInspectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = create_project('sample', directory=Path(self.tmp.name), modular=True)

    def call(self, *argv):
        buf = io.StringIO()
        with patch('pathlib.Path.cwd', return_value=self.root), contextlib.redirect_stdout(buf):
            self.assertEqual(main(list(argv)), 0)
        return buf.getvalue()

    def test_controller_list(self):
        self.assertIn('Blog', self.call('controller','list','blog'))

    def test_controller_show(self):
        out=self.call('controller','show','blog','blog')
        self.assertIn('Controller: Blog',out)
        self.assertIn('Blog.py',out)

    def test_view_list(self):
        self.assertIn('index.html', self.call('view','list','blog'))

    def test_view_show_with_or_without_extension(self):
        for name in ('index','index.html'):
            self.assertIn('index.html',self.call('view','show','blog',name))

    def test_missing_component_fails(self):
        with patch('pathlib.Path.cwd',return_value=self.root),contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(['controller','show','blog','missing'])
        self.assertEqual(caught.exception.code,1)

    def test_no_imports_required(self):
        (self.root/'apps/blog/controllers/broken.py').write_text('raise RuntimeError("not imported")')
        self.assertIn('broken',list_components('controller','blog',directory=self.root))

    def test_invalid_resource_fails_closed(self):
        with self.assertRaises(ValueError):
            list_components('form','blog',directory=self.root)

    def test_symlink_not_followed(self):
        with tempfile.TemporaryDirectory() as outside:
            link=self.root/'apps/blog/controllers/external.py'
            try: link.symlink_to(Path(outside)/'external.py')
            except (OSError,NotImplementedError): self.skipTest('symlink unavailable')
            self.assertNotIn('external',list_components('controller','blog',directory=self.root))

if __name__=='__main__': unittest.main()
