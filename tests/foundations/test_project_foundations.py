"""Workstream A contract tests without database mutations or server binding."""

import json
from unittest.mock import patch

import pytest

from aurora import Aurora
from aurora.cli import main
from aurora._cli_scaffolding import create_project, parse_controller_methods
from aurora.core.app_settings import load_application_settings
from aurora.view import View


def test_server_defaults_validation_and_strict_settings(tmp_path):
    root = create_project("demo", directory=tmp_path, modular=True)
    config = root / "aurora_config.json"
    assert load_application_settings(config).strict is True
    assert dict(load_application_settings(config).server) == {
        "host": "127.0.0.1", "port": 8000, "reload": True,
    }
    original = json.loads(config.read_text())
    valid = {**original, "strict": False, "server": {"port": 9000, "reload": False}}
    config.write_text(json.dumps(valid))
    settings = load_application_settings(config)
    assert settings.strict is False
    assert settings.server["port"] == 9000
    assert settings.server["reload"] is False

    invalid = ({"strict": "false"}, {"server": {"port": True}},
               {"server": {"reload": "false"}}, {"server": {"extra": 1}},
               {"server": {"port": 65536}}, {"server": {"host": ""}})
    for replacement in invalid:
        config.write_text(json.dumps({**original, **replacement}))
        with pytest.raises(ValueError):
            load_application_settings(config)


def test_run_precedence_and_reload_flags(tmp_path, monkeypatch):
    root = create_project("demo", directory=tmp_path)
    monkeypatch.chdir(root)
    config = root / "aurora_config.json"
    document = json.loads(config.read_text())
    document["server"] = {"port": 9000, "reload": False, "host": "0.0.0.0"}
    config.write_text(json.dumps(document))

    with patch("uvicorn.run") as run:
        assert main(["run", "--port", "7000", "--reload"]) == 0
        run.assert_called_once_with("app:app", host="0.0.0.0", port=7000, reload=True)

    with patch("uvicorn.run") as run:
        assert main(["run"]) == 0
        run.assert_called_once_with("app:app", host="0.0.0.0", port=9000, reload=False)

    with pytest.raises(SystemExit) as exc:
        main(["run", "--reload", "--no-reload"])
    assert exc.value.code == 2


def test_methods_and_exact_identity(tmp_path, monkeypatch):
    root = create_project("demo", directory=tmp_path, mvc=True)
    monkeypatch.chdir(root)
    assert parse_controller_methods("GET,post,get,DELETE") == ("get", "post", "delete")
    with pytest.raises(ValueError):
        parse_controller_methods("get,unknown")
    assert main(["controller", "add", "Products", "--methods", "GET,post,get,delete"]) == 0
    source = (root / "controllers" / "Products.py").read_text()
    assert source.count("    def get(self):") == 1
    assert source.index("def get") < source.index("def post") < source.index("def delete")
    with pytest.raises(SystemExit):
        main(["controller", "show", "products"])
    with pytest.raises(SystemExit):
        main(["controller", "remove", "Products"])
    assert (root / "controllers" / "Products.py").exists()


def test_app_namespaces_without_shared_directory(tmp_path):
    root = create_project("demo", directory=tmp_path, modular=True)
    # Generated app-local views need not depend on a shared root.
    import shutil
    shutil.rmtree(root / "views")
    app = Aurora.from_project(root / "app.py")
    assert app.renderer is not None
    (root / "apps" / "blog" / "views" / "simple.html").write_text("Hello {{ title }}")
    assert app.renderer.render(View("blog/simple", title="News")) == "Hello News"
