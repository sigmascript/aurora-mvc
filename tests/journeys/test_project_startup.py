"""Exercise generated project startup forms without binding a listening socket."""

import json
from unittest.mock import patch

import pytest

from aurora._cli_scaffolding import create_project
from aurora.cli import main


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
def test_project_and_parent_startup_use_project_settings(tmp_path, monkeypatch, layout):
    root = create_project("newsroom", directory=tmp_path,
                          mvc=layout == "mvc", modular=layout == "modular")
    document = json.loads((root / "aurora_config.json").read_text())
    document["server"] = {"host": "127.0.0.1", "port": 9137, "reload": False}
    (root / "aurora_config.json").write_text(json.dumps(document))

    monkeypatch.chdir(root)
    with patch("uvicorn.run") as run:
        assert main(["run"]) == 0
        run.assert_called_once_with("app:app", host="127.0.0.1", port=9137, reload=False)

    monkeypatch.chdir(tmp_path)
    with patch("uvicorn.run") as run:
        assert main(["run", "newsroom.app:app", "--port", "9140"]) == 0
        run.assert_called_once_with("newsroom.app:app", host="127.0.0.1", port=9140,
                                    reload=False)

    guide = (root / "README.md").read_text()
    assert "aurora run newsroom.app:app" in guide
    assert "aurora_config.json" in guide


def test_parent_startup_fails_closed_on_invalid_project_settings(tmp_path, monkeypatch, capsys):
    root = create_project("newsroom", directory=tmp_path)
    (root / "aurora_config.json").write_text('{"server": {"port": "oops"}}')
    monkeypatch.chdir(tmp_path)
    with patch("uvicorn.run") as run, pytest.raises(SystemExit) as error:
        main(["run", "newsroom.app:app"])
    assert error.value.code == 1
    assert "Invalid application configuration" in capsys.readouterr().err
    run.assert_not_called()


def test_parent_startup_missing_module_fails_before_server(tmp_path, monkeypatch, capsys):
    create_project("newsroom", directory=tmp_path)
    monkeypatch.chdir(tmp_path)
    with patch("uvicorn.run") as run, pytest.raises(SystemExit) as error:
        main(["run", "newsroom.missing:app"])
    assert error.value.code == 1
    assert "Cannot find project application module" in capsys.readouterr().err
    run.assert_not_called()


def test_simple_startup_rejects_stale_import_cache(tmp_path, monkeypatch, capsys):
    """A previously imported `app` cannot satisfy another project's preflight."""
    import sys
    from types import ModuleType

    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "app", ModuleType("app"))
    with patch("uvicorn.run") as run, pytest.raises(SystemExit) as error:
        main(["run"])
    assert error.value.code == 1
    assert "Cannot find application module 'app'" in capsys.readouterr().err
    run.assert_not_called()


def test_simple_startup_checks_local_file_even_with_cached_app(tmp_path, monkeypatch):
    import sys
    from types import ModuleType

    root = create_project("newsroom", directory=tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setitem(sys.modules, "app", ModuleType("app"))
    with patch("uvicorn.run") as run:
        assert main(["run"]) == 0
    run.assert_called_once()
