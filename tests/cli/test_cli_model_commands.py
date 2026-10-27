"""Phase 6.9: model scaffolding is isolated, explicit and non-destructive."""
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from aurora.cli import create_project, create_model, list_components, main, show_component

class ModelCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = create_project('sample', directory=Path(self.tmp.name), modular=True)

    def invoke(self, *args):
        out = io.StringIO()
        with patch('pathlib.Path.cwd', return_value=self.root), contextlib.redirect_stdout(out):
            self.assertEqual(main(list(args)), 0)
        return out.getvalue()

    def test_add_scaffolds_model(self):
        out = self.invoke('model', 'add', 'blog', 'BlogPost')
        path = self.root / 'apps/blog/models/BlogPost.py'
        self.assertTrue(path.is_file())
        self.assertIn('class BlogPost(Model)', path.read_text())
        self.assertIn('database unchanged', out)
        self.assertFalse((self.root/'apps/blog/models/__init__.py').exists())

    def test_list_and_show(self):
        self.invoke('model', 'add', 'blog', 'BlogPost')
        self.assertIn('BlogPost', self.invoke('model', 'list', 'blog'))
        self.assertIn('BlogPost.py', self.invoke('model', 'show', 'blog', 'BlogPost'))

    def test_missing_models_directory_lists_empty(self):
        self.assertEqual(list_components('model', 'blog', directory=self.root), [])

    def test_no_overwrite(self):
        path = create_model('blog','BlogPost',directory=self.root)
        path.write_text('developer work')
        with self.assertRaises(FileExistsError):
            create_model('blog','BlogPost',directory=self.root)
        self.assertEqual(path.read_text(), 'developer work')

    def test_invalid_model_name(self):
        with self.assertRaises(ValueError):
            create_model('blog', '../bad', directory=self.root)

    def test_no_project_or_database_mutation(self):
        original = (self.root/'app.py').read_bytes()
        self.invoke('model','add','blog','Article')
        self.assertEqual((self.root/'app.py').read_bytes(), original)
        self.assertFalse(list(self.root.rglob('*.sqlite3')))

    def test_production_rejected(self):
        with patch.dict(os.environ, {'AURORA_ENV':'production'}), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                self.invoke('model','add','blog','Article')
        self.assertEqual(caught.exception.code,1)

    def test_model_not_imported_on_inspection(self):
        self.invoke('model','add','blog','Article')
        path = self.root/'apps/blog/models/Article.py'
        path.write_text('raise RuntimeError("not imported")')
        self.assertIn('Article', list_components('model','blog',directory=self.root))
        self.assertEqual(show_component('model','blog','Article',directory=self.root)['name'],'Article')

if __name__=='__main__': unittest.main()
