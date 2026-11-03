"""Alpha 4 naming, route, compatibility and non-destructive scaffolding contracts."""

from pathlib import Path
import tempfile
import unittest

from aurora.cli import (
    create_project, create_controller, create_standalone_controller,
    create_model, create_shared_component, create_view,
    show_component, show_shared_component, show_standalone_controller,
)


class Alpha4ScaffoldingContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def test_standalone_camelcase_route_and_legacy_lookup(self):
        root = create_project("site", directory=self.base, mvc=True)
        path = create_standalone_controller("AccountSettings", directory=root)
        self.assertEqual(path.name, "AccountSettings.py")
        self.assertIn('app.route("/account-settings/", AccountSettings)', (root / "app.py").read_text())
        self.assertEqual(Path(show_standalone_controller("AccountSettings", directory=root)["path"]).resolve(), path.resolve())
        old = root / "controllers" / "legacy.py"
        old.write_text("# existing developer file\n")
        self.assertEqual(Path(show_standalone_controller("Legacy", directory=root)["path"]).resolve(), old.resolve())
        before = (root / "app.py").read_bytes()
        with self.assertRaises(FileExistsError):
            create_standalone_controller("Legacy", directory=root)
        self.assertEqual((root / "app.py").read_bytes(), before)

    def test_modular_camelcase_routes_and_legacy_collision(self):
        root = create_project("site", directory=self.base, modular=True)
        path = create_controller("blog", "AccountSettings", directory=root)
        self.assertEqual(path.name, "AccountSettings.py")
        self.assertIn('app.route("/blog/account-settings/", AccountSettings)', (root / "app.py").read_text())
        self.assertEqual(Path(show_component("controller", "blog", "AccountSettings", directory=root)["path"]).resolve(), path.resolve())
        old = root / "apps" / "blog" / "controllers" / "legacy.py"
        old.write_text("# legacy\n")
        with self.assertRaises(FileExistsError):
            create_controller("blog", "Legacy", directory=root)
        self.assertEqual(Path(show_component("controller", "blog", "Legacy", directory=root)["path"]).resolve(), old.resolve())

    def test_models_camelcase_and_legacy_snake_case_lookup(self):
        root = create_project("site", directory=self.base, modular=True)
        path = create_model("blog", "BlogPost", directory=root)
        self.assertEqual(path.name, "BlogPost.py")
        self.assertEqual(Path(show_component("model", "blog", "BlogPost", directory=root)["path"]).resolve(), path.resolve())
        old = root / "apps" / "blog" / "models" / "OldEntry.py"
        old.write_text("# original\n")
        shared = create_shared_component("model", "AccountSettings", directory=root)
        self.assertEqual(shared.name, "AccountSettings.py")
        legacy = root / "models" / "old_entry.py"
        legacy.write_text("# legacy\n")
        self.assertEqual(Path(show_shared_component("model", "OldEntry", directory=root)["path"]).resolve(), legacy.resolve())
        with self.assertRaises(FileExistsError):
            create_shared_component("model", "OldEntry", directory=root)

    def test_views_include_title_blocks_and_starter_routes_are_exact(self):
        root = create_project("site", directory=self.base, modular=True)
        self.assertIn('app.route("/blog/", Blog)', (root / "app.py").read_text())
        self.assertNotIn('app.route("/blog", Blog)', (root / "app.py").read_text())
        for path in (create_view("blog", "contact", directory=root),
                     create_shared_component("view", "contact", directory=root),
                     root / "apps/blog/views/index.html"):
            self.assertIn("{% block title %}", path.read_text())
