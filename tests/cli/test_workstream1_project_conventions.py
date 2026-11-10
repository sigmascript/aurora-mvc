"""Developer journeys for convention-based scaffolds and safe removal."""

import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from aurora._cli_scaffolding import (
    create_project, add_app, remove_component, create_controller,
    create_shared_component, create_model, create_standalone_controller,
)
from aurora.core.app_settings import load_application_settings


def test_three_layouts_and_automatic_modular_views(tmp_path):
    for mode in ("minimal", "mvc", "modular"):
        root = create_project(mode, directory=tmp_path,
                              mvc=mode == "mvc", modular=mode == "modular")
        source = (root / "app.py").read_text(encoding="utf-8")
        assert "ROOT =" not in source
        assert "Aurora.from_project(__file__)" in source
        assert "format" not in json.loads((root / "aurora_config.json").read_text())
        assert "aurora run" in (root / "README.md").read_text()
        assert not (root / "backups").exists()
        assert not (root / "migrations").exists()

        if mode == "minimal":
            assert not (root / "aurora_databases.json").exists()
        if mode == "modular":
            add_app("news", directory=root)
            assert "view_namespaces" not in (root / "app.py").read_text()

            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            result = subprocess.run(
                [sys.executable, "-c", "import app; from aurora.view import View; "
                 "assert 'Your new Aurora app' in app.app.renderer.render(View('news/index', title='News')); "
                 "assert app.app.static_directory is not None"],
                cwd=root, env=environment, capture_output=True, text=True, check=False,
            )
            assert result.returncode == 0, result.stderr


def test_model_names_are_unique_within_one_database(tmp_path):
    root = create_project("example", directory=tmp_path, modular=True)
    shared = create_shared_component("model", "Invoice", directory=root)
    with pytest.raises(ValueError, match="already exists for database 'default'"):
        create_model("blog", "Invoice", directory=root)
    assert shared.exists()
    assert not (root / "apps/blog/models/Invoice.py").exists()

    dedicated = create_model("blog", "Receipt", directory=root)
    with pytest.raises(ValueError, match="already exists for database 'default'"):
        create_shared_component("model", "Receipt", directory=root)
    assert dedicated.exists()
    assert not (root / "models/Receipt.py").exists()

    manifest = root / "aurora_models.json"
    manifest.write_text(json.dumps({"databases": {
        "analytics": ["apps.blog.models.Receipt:Receipt"]
    }}))
    assert create_shared_component("model", "Receipt", directory=root).exists()


def test_modular_registration_requires_no_comment_markers(tmp_path):
    root = create_project("example", directory=tmp_path, modular=True)
    assert "# aurora:imports" not in (root / "app.py").read_text()
    assert "# aurora:routes" not in (root / "app.py").read_text()
    create_controller("blog", "Archive", directory=root)
    add_app("news", directory=root)
    source = (root / "app.py").read_text()
    assert 'app.route("/blog/archive/", Archive)' in source
    assert 'app.route("/news/", News)' in source
    assert "# aurora:" not in source


def test_standalone_controller_import_spacing(tmp_path):
    root = create_project("example", directory=tmp_path, mvc=True)
    create_standalone_controller("About", directory=root)
    source = (root / "app.py").read_text()
    assert "from controllers.Home import Home\nfrom controllers.About import About\n\n\napp =" in source


def test_optional_static_directory_has_safe_single_name(tmp_path):
    root = create_project("example", directory=tmp_path, mvc=True)
    path = root / "aurora_config.json"
    data = json.loads(path.read_text())
    data["statics"] = "public"
    path.write_text(json.dumps(data))
    assert load_application_settings(path).statics == "public"

    for unsafe in ("../private", "/absolute", ".hidden", "a/b"):
        data["statics"] = unsafe
        path.write_text(json.dumps(data))

        with pytest.raises(ValueError, match="statics"):
            load_application_settings(path)


def test_remove_generated_controller_and_model_without_database_write(tmp_path):
    root = create_project("example", directory=tmp_path, mvc=True)
    from aurora._cli_scaffolding import create_standalone_controller, create_shared_component
    from aurora.database.cli_registration import register_generated_model

    controller = create_standalone_controller("Profile", directory=root)
    model = create_shared_component("model", "User", directory=root)
    register_generated_model(model, "User", project=root)

    with patch("builtins.input", return_value="n"):
        assert remove_component("controller", "Profile", directory=root) is None

    assert controller.exists()

    with patch("builtins.input", return_value="y"):
        assert remove_component("controller", "Profile", directory=root) == controller
        assert remove_component("model", "User", directory=root) == model

    assert not controller.exists()
    assert not model.exists()
    assert 'app.route("/profile/", Profile)' not in (root / "app.py").read_text()
    manifest = json.loads((root / "aurora_models.json").read_text())
    assert all("models.User:User" not in refs for refs in manifest["databases"].values())
    assert not (root / "data.sqlite3").exists()


def test_public_remove_command_confirms_exact_target(tmp_path, monkeypatch, capsys):
    from aurora.cli import main
    from aurora._cli_scaffolding import create_standalone_controller

    root = create_project("example", directory=tmp_path, mvc=True)
    controller = create_standalone_controller("Profile", directory=root)
    monkeypatch.chdir(root)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")

    assert main(["controller", "remove", "Profile"]) == 0
    assert not controller.exists()
    assert str(controller) in capsys.readouterr().out
