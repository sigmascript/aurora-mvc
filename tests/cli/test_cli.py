"""Aurora CLI scaffold contract."""
import importlib.util
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from aurora.cli import create_project, main


class TestCLI(unittest.TestCase):
    def test_scaffold_is_minimal_and_importable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("myproject", directory=Path(tmp))
            self.assertTrue((root / "__init__.py").exists())
            self.assertIn("app = Aurora()", (root / "app.py").read_text())
            spec = importlib.util.spec_from_file_location("generated_app", root / "app.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.assertIsNotNone(module.app)

    def test_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            create_project("myproject", directory=Path(tmp))
            with self.assertRaises(FileExistsError):
                create_project("myproject", directory=Path(tmp))

    def test_invalid_project_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("bad-name", "a/b", "2bad", "aurora"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    create_project(name, directory=Path(tmp))

    def test_cli_new(self):
        with tempfile.TemporaryDirectory() as tmp, patch("aurora.cli.Path.cwd", return_value=Path(tmp)):
            self.assertEqual(main(["new", "sample"]), 0)
            self.assertTrue((Path(tmp) / "sample" / "app.py").is_file())

    def test_run_arguments(self):
        with patch("uvicorn.run") as run:
            self.assertEqual(main(["run", "sample.app:app", "--port", "9001", "--no-reload"]), 0)
            run.assert_called_once_with("sample.app:app", host="127.0.0.1", port=9001, reload=False)

    def test_run_adds_current_project_to_import_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = create_project("hello_aurora", directory=Path(tmp))
            with patch("aurora.cli.Path.cwd", return_value=project), patch("uvicorn.run") as run:
                self.assertEqual(main(["run"]), 0)
                self.assertEqual(sys.path[0], str(project.resolve()))
                run.assert_called_once()
            sys.path.remove(str(project.resolve()))


class TestModularMultipleApps(unittest.TestCase):
    def test_modular_scaffold_has_explicit_routes_and_view_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            source = (root / "app.py").read_text(encoding="utf-8")
            self.assertIn('app.route("/blog", Blog)', source)
            self.assertIn('ROOT / "views"', source)
            self.assertTrue((root / "views/layout.html").is_file())
            self.assertTrue((root / "apps/home/views/index.html").is_file())
            self.assertTrue((root / "apps/blog/views/index.html").is_file())

    def test_modular_views_render_shared_layout(self):
        from aurora.rendering import ViewRenderer
        from aurora.view import View
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            renderer = ViewRenderer(root / "views", view_namespaces={"home": root / "apps/home/views", "blog": root / "apps/blog/views"})
            home = renderer.render(View("home/index", title="Home"))
            blog = renderer.render(View("blog/index", title="Blog"))
            self.assertIn("Your modular Aurora project is ready", home)
            self.assertIn('href="/blog"', home)
            self.assertIn("Your blog app is ready", blog)
            self.assertIn('href="/"', blog)

    def test_modular_view_names_do_not_collide(self):
        from aurora.rendering import ViewRenderer
        from aurora.view import View
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            renderer = ViewRenderer(root / "views", view_namespaces={"home": root / "apps/home/views", "blog": root / "apps/blog/views"})
            self.assertNotEqual(
                renderer.render(View("home/index", title="X")),
                renderer.render(View("blog/index", title="X")),
            )

class TestAddModularApp(unittest.TestCase):
    def test_add_app_creates_controller_view_and_explicit_route(self):
        from aurora.cli import add_app
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            created = add_app("news_feed", directory=root)
            self.assertTrue((created / "controllers/news_feed.py").is_file())
            self.assertTrue((created / "views/index.html").is_file())
            source = (root / "app.py").read_text()
            self.assertIn('app.route("/news_feed", NewsFeed)', source)
            self.assertIn('"news_feed": ROOT / "apps" / "news_feed" / "views"', source)
            from aurora.rendering import ViewRenderer
            from aurora.view import View
            renderer = ViewRenderer(root / "views", view_namespaces={"news_feed": created / "views"})
            self.assertIn("Your new Aurora app is ready", renderer.render(View("news_feed/index", title="News Feed")))

    def test_add_app_rejects_duplicates_without_changes(self):
        from aurora.cli import add_app
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            add_app("news", directory=root)
            source = (root / "app.py").read_text()
            with self.assertRaises(FileExistsError):
                add_app("news", directory=root)
            self.assertEqual(source, (root / "app.py").read_text())

    def test_add_app_refuses_unmarked_project(self):
        from aurora.cli import add_app
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            source = (root / "app.py").read_text().replace("# aurora:routes", "")
            (root / "app.py").write_text(source)
            with self.assertRaises(ValueError):
                add_app("news", directory=root)
            self.assertFalse((root / "apps/news").exists())

    def test_cli_add_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            with patch("aurora.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["add-app", "news"]), 0)
            self.assertTrue((root / "apps/news/controllers/news.py").is_file())

class TestAppCrudRead(unittest.TestCase):
    def test_list_apps_and_show_app(self):
        from aurora.cli import list_apps, show_app
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            self.assertEqual(list_apps(directory=root), ["blog", "home"])
            info = show_app("blog", directory=root)
            self.assertEqual(info["controllers"], ["blog"])
            self.assertEqual(info["views"], ["index.html"])

    def test_create_app_command_and_compatibility_alias(self):
        from aurora.cli import list_apps
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            with patch("aurora.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["create-app", "news"]), 0)
                self.assertEqual(main(["add-app", "store"]), 0)
            self.assertEqual(list_apps(directory=root), ["blog", "home", "news", "store"])

    def test_cli_list_and_show(self):
        from io import StringIO
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            with patch("aurora.cli.Path.cwd", return_value=root), patch("sys.stdout", new_callable=StringIO) as out:
                self.assertEqual(main(["list-apps"]), 0)
                self.assertEqual(main(["show-app", "home"]), 0)
                self.assertIn("blog", out.getvalue())
                self.assertIn("index.html", out.getvalue())

    def test_invalid_app_read_and_non_modular_project(self):
        from aurora.cli import list_apps, show_app
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("multi", directory=Path(tmp), modular=True)
            with self.assertRaises(ValueError):
                show_app("../secrets", directory=root)
            with self.assertRaises(ValueError):
                show_app("missing", directory=root)
            with self.assertRaises(ValueError):
                list_apps(directory=Path(tmp))



class TestStarterStaticsIntegration(unittest.TestCase):
    def test_mvc_starter_has_referenced_stylesheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("site", directory=Path(tmp), mvc=True)
            self.assertIn('{% extends "layout.html" %}', (root / 'views/home.html').read_text())
            self.assertIn('/statics/css/site.css', (root / 'views/layout.html').read_text())
            self.assertIn('font-family', (root / 'statics/css/site.css').read_text())
            self.assertIn('statics_directory=', (root / 'app.py').read_text())

    def test_modular_starter_uses_shared_stylesheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("site", directory=Path(tmp), modular=True)
            self.assertIn('/statics/css/site.css', (root / 'views/layout.html').read_text())
            self.assertTrue((root / 'statics/css/site.css').is_file())
            self.assertIn('statics_directory=', (root / 'app.py').read_text())

    def test_minimal_starter_does_not_require_statics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = create_project("site", directory=Path(tmp))
            self.assertFalse((root / 'statics').exists())
