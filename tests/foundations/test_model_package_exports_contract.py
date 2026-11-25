"""Package-local model exports respect developer code and CLI operations."""

import json
import os
import subprocess
import sys

import pytest

from aurora._cli_scaffolding import create_project
from aurora.cli import main


def _invoke_import(project, command):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        (
            str(project),
            str(__import__("pathlib").Path(__file__).resolve().parents[2]),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", command], cwd=project, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("modular", [False, True])
def test_shared_models_export_classes_and_preserve_custom_code(tmp_path, monkeypatch, modular):
    root = create_project("sample", directory=tmp_path, mvc=True, modular=modular)
    package = root / "models/__init__.py"
    package.write_text('"""Developer package."""\nCUSTOM = 7\n', encoding="utf-8")
    monkeypatch.chdir(root)
    assert main(["model", "add", "Notes"]) == 0
    assert main(["model", "add", "Users"]) == 0
    source = package.read_text()
    assert '"""Developer package."""' in source and "CUSTOM = 7" in source
    _invoke_import(
        root,
        "from models import Notes, Users, CUSTOM; "
        "assert isinstance(Notes, type) and isinstance(Users, type) and CUSTOM == 7",
    )
    settings = json.loads((root / "aurora_config.json").read_text())
    settings["strict"] = False
    (root / "aurora_config.json").write_text(json.dumps(settings))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["model", "remove", "Notes"]) == 0
    assert "from .Notes import Notes" not in package.read_text()
    assert "from .Users import Users" in package.read_text()
    _invoke_import(
        root, "from models import Users, CUSTOM; assert isinstance(Users, type) and CUSTOM == 7"
    )
    assert main(["model", "add", "Notes"]) == 0
    _invoke_import(root, "from models import Notes; assert isinstance(Notes, type)")


def test_modular_app_exports_are_package_local(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, modular=True)
    monkeypatch.chdir(root)
    assert main(["model", "add", "Users"]) == 0
    assert main(["model", "add", "Posts", "--app", "blog"]) == 0
    assert main(["model", "add", "Notes", "--app", "home"]) == 0
    assert "Posts" not in (root / "models/__init__.py").read_text()
    assert "Notes" not in (root / "apps/blog/models/__init__.py").read_text()
    _invoke_import(
        root,
        "from models import Users; from apps.blog.models import Posts; "
        "from apps.home.models import Notes; "
        "assert all(isinstance(x, type) for x in (Users, Posts, Notes))",
    )
    settings = json.loads((root / "aurora_config.json").read_text())
    settings["strict"] = False
    (root / "aurora_config.json").write_text(json.dumps(settings))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["model", "remove", "Posts", "--app", "blog"]) == 0
    assert "Posts" not in (root / "apps/blog/models/__init__.py").read_text()
    assert "Users" in (root / "models/__init__.py").read_text()
    assert "Notes" in (root / "apps/home/models/__init__.py").read_text()


def test_custom_export_collision_refuses_without_changes(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    package = root / "models/__init__.py"
    package.write_text("Users = 'custom'\n", encoding="utf-8")
    manifest = (root / "aurora_models.json").read_bytes()
    monkeypatch.chdir(root)
    with pytest.raises(SystemExit):
        main(["model", "add", "Users"])
    assert package.read_text() == "Users = 'custom'\n"
    assert (root / "aurora_models.json").read_bytes() == manifest
    assert not (root / "models/Users.py").exists()


def test_removed_model_custom_import_refuses_without_changes(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    monkeypatch.chdir(root)
    assert main(["model", "add", "Notes"]) == 0
    package = root / "models/__init__.py"
    package.write_text("from .Notes import Notes\n", encoding="utf-8")
    settings = json.loads((root / "aurora_config.json").read_text())
    settings["strict"] = False
    (root / "aurora_config.json").write_text(json.dumps(settings))
    manifest = (root / "aurora_models.json").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Must refuse before prompt"))
    with pytest.raises(ValueError, match="custom"):
        from aurora._cli_scaffolding import remove_component

        remove_component("model", "Notes")
    assert package.read_text() == "from .Notes import Notes\n"
    assert (root / "models/Notes.py").exists()
    assert (root / "aurora_models.json").read_bytes() == manifest


def test_public_jsonresponse_export():
    from aurora import JSONResponse
    from aurora.core.response import JSONResponse as Original

    assert JSONResponse is Original


def test_refuses_export_changed_after_preflight(tmp_path, monkeypatch):
    from aurora import _model_exports

    root = create_project("sample", directory=tmp_path, mvc=True)
    monkeypatch.chdir(root)
    package = root / "models/__init__.py"
    manifest = (root / "aurora_models.json").read_bytes()
    publish = _model_exports.write_model_export

    def concurrent_edit(path, data, *, expected=None):
        path.write_text("CUSTOM = 'preserve'\n# concurrent change\n", encoding="utf-8")
        return publish(path, data, expected=expected)

    monkeypatch.setattr(_model_exports, "write_model_export", concurrent_edit)
    with pytest.raises(ValueError, match="changed since export review"):
        _model_exports.write_model_export(package, b"from .Notes import Notes\n", expected=b"")
    package.write_text("", encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["model", "add", "Notes"])
    assert not (root / "models/Notes.py").exists()
    assert (root / "aurora_models.json").read_bytes() == manifest
    assert package.read_text() == "CUSTOM = 'preserve'\n# concurrent change\n"


def test_windows_crlf_package_preserved_across_add_and_remove(tmp_path, monkeypatch):
    root = create_project("sample", directory=tmp_path, mvc=True)
    package = root / "models/__init__.py"
    custom = b'"""Developer package."""\r\nCUSTOM = 7\r\n'
    package.write_bytes(custom)
    monkeypatch.chdir(root)

    assert main(["model", "add", "Notes"]) == 0
    updated = package.read_bytes()
    assert updated.startswith(custom.rstrip(b"\r\n"))
    assert b"\r\nfrom .Notes import Notes\r\n" in updated
    assert b"\r\n" in updated and b"\n" not in updated.replace(b"\r\n", b"")

    settings = json.loads((root / "aurora_config.json").read_text())
    settings["strict"] = False
    (root / "aurora_config.json").write_text(json.dumps(settings))
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["model", "remove", "Notes"]) == 0
    assert package.read_bytes() == custom
