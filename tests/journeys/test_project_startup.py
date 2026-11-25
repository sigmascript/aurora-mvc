"""Exercise generated project startup forms without binding a listening socket."""

import json
from unittest.mock import patch

import pytest

from aurora._cli_scaffolding import create_project
from aurora.cli import main


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
def test_project_and_parent_startup_use_project_settings(tmp_path, monkeypatch, layout):
    root = create_project(
        "newsroom", directory=tmp_path, mvc=layout == "mvc", modular=layout == "modular"
    )
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
        run.assert_called_once_with("newsroom.app:app", host="127.0.0.1", port=9140, reload=False)

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


@pytest.mark.parametrize("layout", ["minimal", "mvc", "modular"])
@pytest.mark.parametrize("parent", [False, True])
@pytest.mark.parametrize("reload", [False, True])
def test_generated_startup_imports_real_application(tmp_path, layout, parent, reload):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = create_project(
        "newsroom", directory=tmp_path, mvc=layout == "mvc", modular=layout == "modular"
    )
    if layout == "modular":
        from aurora._cli_scaffolding import create_standalone_controller

        create_standalone_controller("About", directory=root)

    # A competing parent package must not satisfy project-local imports.
    for name in ("controllers", "apps"):
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text('raise RuntimeError("Wrong project imported")\n')

    script = """
import os
import sys
from pathlib import Path
from unittest.mock import patch
from uvicorn import Config
from aurora.cli import main

root = Path(sys.argv[1]).resolve()
original_directory = Path.cwd()
original_path = sys.path[:]

def load(target, **options):
    assert Path.cwd() == root
    assert Path(sys.path[0]) == root
    config = Config(target, **options)
    config.load()
    assert config.loaded_app is not None

with patch("uvicorn.run", side_effect=load):
    assert main(sys.argv[2:]) == 0
assert Path.cwd() == original_directory
assert sys.path == original_path
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    target = "newsroom.app:app" if parent else "app:app"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(root),
            "run",
            target,
            "--reload" if reload else "--no-reload",
        ],
        cwd=tmp_path if parent else root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(root.rglob("*.sqlite3"))
    assert not (root / "migrations").exists()
    assert not (root / "backups").exists()


def test_startup_restores_context_on_server_failure(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    root = create_project("newsroom", directory=tmp_path, mvc=True)
    monkeypatch.chdir(tmp_path)
    original_path = sys.path[:]

    def fail(*args, **kwargs):
        assert Path.cwd() == root
        assert sys.path[0] == str(root)
        raise RuntimeError("Server failed")

    with patch("uvicorn.run", side_effect=fail), pytest.raises(RuntimeError, match="Server failed"):
        main(["run", "newsroom.app:app"])
    assert Path.cwd() == tmp_path
    assert sys.path == original_path


def test_non_aurora_package_keeps_invoking_directory(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    package = tmp_path / "external"
    package.mkdir()
    (package / "__init__.py").touch()
    (package / "server.py").touch()
    monkeypatch.chdir(tmp_path)
    original_path = sys.path[:]

    def run(*args, **kwargs):
        assert Path.cwd() == tmp_path
        assert str(package) not in sys.path

    with patch("uvicorn.run", side_effect=run):
        assert main(["run", "external.server:app"]) == 0
    assert sys.path == original_path
