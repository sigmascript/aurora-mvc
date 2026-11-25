"""Generated-project CLI workflows and isolated ASGI response journeys."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from aurora.cli import main

_ASGI = """import asyncio, json
from app import app
async def run():
    results=[]
    for path in PATHS:
        messages=[]
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(event):
            messages.append(event)
        await app({"type": "http", "asgi": {"version": "3.0"},
                   "http_version": "1.1", "method": "GET", "path": path,
                   "raw_path": path.encode(), "root_path": "", "query_string": b"",
                   "headers": [], "scheme": "http", "server": ("localhost", 80),
                   "client": ("127.0.0.1", 10000)}, receive, send)
        results.append((messages[0]["status"], b"".join(m.get("body", b"") for m in messages[1:]).decode()))
    print(json.dumps(results))
asyncio.run(run())
"""


def responses(root, paths):
    # A separate process also checks that a newly generated project imports cleanly.
    environment = os.environ.copy()
    repository = str(Path(__file__).resolve().parents[2])
    environment["PYTHONPATH"] = repository + os.pathsep + environment.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", _ASGI.replace("PATHS", repr(paths))],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip())


@pytest.mark.parametrize(
    "option,paths,content",
    [
        (None, ["/"], ["Hello from newsroom"]),
        ("--mvc", ["/"], ["Welcome to newsroom"]),
        ("--modular", ["/", "/blog/"], ["Welcome to newsroom", "Your blog app is ready"]),
    ],
)
def test_new_and_serve_full_project_without_database(tmp_path, monkeypatch, option, paths, content):
    monkeypatch.chdir(tmp_path)
    argv = ["new", "newsroom"] + ([option] if option else [])
    assert main(argv) == 0
    root = tmp_path / "newsroom"
    assert root.is_dir()
    assert (root / "aurora_config.json").is_file()
    for (status, body), expected in zip(responses(root, paths), content):
        assert status == 200
        assert expected in body
    assert not (root / "data.sqlite3").exists()
    assert not (root / "migrations").exists()
    assert not (root / "backups").exists()
    with pytest.raises(SystemExit) as error:
        main(argv)
    assert error.value.code == 1
    assert (root / "app.py").is_file()


def test_modular_app_lifecycle_changes_real_http_routes_safely(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "newsroom", "--modular"]) == 0
    root = tmp_path / "newsroom"
    monkeypatch.chdir(root)
    assert main(["app", "add", "accounts"]) == 0
    assert "Route: /accounts/" in capsys.readouterr().out.splitlines()
    assert main(["app", "show", "accounts"]) == 0
    assert responses(root, ["/accounts/"])[0][0] == 200
    original = (root / "app.py").read_bytes()
    with pytest.raises(SystemExit) as error:
        main(["app", "remove", "accounts"])
    assert error.value.code == 1
    assert (root / "app.py").read_bytes() == original
    settings = root / "aurora_config.json"
    document = json.loads(settings.read_text(encoding="utf-8"))
    document["strict"] = False
    settings.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["app", "remove", "accounts"]) == 0
    assert responses(root, ["/accounts/"])[0][0] == 200
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["app", "remove", "accounts"]) == 0
    assert not (root / "apps/accounts").exists()
    assert responses(root, ["/accounts/", "/blog/"])[0][0] == 404
    assert responses(root, ["/blog/"])[0][0] == 200
    assert not (root / "data.sqlite3").exists()


def test_generated_controller_methods_do_not_bypass_strict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "newsroom", "--mvc"]) == 0
    root = tmp_path / "newsroom"
    monkeypatch.chdir(root)
    assert main(["controller", "add", "Products", "--methods", "get,post,DELETE"]) == 0
    assert main(["controller", "show", "Products"]) == 0
    source = (root / "controllers/Products.py").read_bytes()
    assert b"def get(" in source and b"def post(" in source and b"def delete(" in source
    with pytest.raises(SystemExit):
        main(["controller", "show", "products"])
    with pytest.raises(SystemExit):
        main(["controller", "remove", "Products"])
    assert (root / "controllers/Products.py").read_bytes() == source
    assert not (root / "data.sqlite3").exists()


def test_unknown_cache_file_refuses_without_deleting_app(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "newsroom", "--modular"]) == 0
    root = tmp_path / "newsroom"
    monkeypatch.chdir(root)
    assert main(["app", "add", "accounts"]) == 0
    cache = root / "apps/accounts/controllers/__pycache__"
    cache.mkdir()
    (cache / "unrecognized.pyc").write_bytes(b"precious")
    settings = root / "aurora_config.json"
    document = json.loads(settings.read_text())
    document["strict"] = False
    settings.write_text(json.dumps(document))
    original = (root / "app.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unsafe removal must not prompt"))
    with pytest.raises(SystemExit) as error:
        main(["app", "remove", "accounts"])
    assert error.value.code == 1
    assert (cache / "unrecognized.pyc").read_bytes() == b"precious"
    assert (root / "app.py").read_bytes() == original


def test_modular_shared_controller_lifecycle(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "newsroom", "--modular"]) == 0
    root = tmp_path / "newsroom"
    monkeypatch.chdir(root)
    package = root / "controllers"
    entry = root / "app.py"
    original = entry.read_bytes()
    assert main(["controller", "list"]) == 0
    assert "No controllers found." in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["controller", "show", "About"])
    with pytest.raises(SystemExit):
        main(["controller", "add", "Blog"])
    assert not package.exists()
    assert entry.read_bytes() == original
    assert main(["controller", "add", "blog", "About"]) == 0
    assert main(["controller", "add", "About", "--methods", "get,post"]) == 0
    target = package / "About.py"
    local = root / "apps/blog/controllers/About.py"
    assert (package / "__init__.py").is_file()
    assert b"def post(" in target.read_bytes()
    assert main(["controller", "list"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "About"
    assert main(["controller", "show", "About"]) == 0
    assert "App: (shared)" in capsys.readouterr().out
    before = (entry.read_bytes(), target.read_bytes(), local.read_bytes())
    for command in (["add", "About"], ["show", "about"], ["remove", "About"]):
        with pytest.raises(SystemExit):
            main(["controller", *command])
    assert (entry.read_bytes(), target.read_bytes(), local.read_bytes()) == before
    assert [
        status for status, _ in responses(root, ["/", "/blog/", "/about/", "/blog/about/"])
    ] == [200] * 4
    settings = root / "aurora_config.json"
    config = json.loads(settings.read_text())
    config["strict"] = False
    settings.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit):
        main(["controller", "remove", "about"])
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["controller", "remove", "About"]) == 0
    assert (entry.read_bytes(), target.read_bytes(), local.read_bytes()) == before
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Unsafe removal must not prompt"))
    for reference in (
        b"\nfrom controllers.About import About as Shared\n",
        b"\nfrom controllers import (\n    About as Shared,\n)\n",
        b"\nfrom newsroom.controllers.About import About as Shared\n",
    ):
        local.write_bytes(before[2] + reference)
        with pytest.raises(SystemExit):
            main(["controller", "remove", "About"])
        assert target.read_bytes() == before[1]
    local.write_bytes(before[2])
    for route in (
        'app.route("/about/", SharedAbout, name="about")',
        'app.route("/custom/", SharedAbout)',
        'app.route("/about/", SharedAbout); developer_setting = True',
    ):
        original_route = 'app.route("/about/", SharedAbout)'
        customized = before[0].decode().replace(original_route, route)
        assert customized != before[0].decode()
        entry.write_text(customized, encoding="utf-8")
        expected = entry.read_bytes()
        with pytest.raises(SystemExit):
            main(["controller", "remove", "About"])
        assert target.read_bytes() == before[1]
        assert entry.read_bytes() == expected
    entry.write_bytes(before[0])
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["controller", "remove", "About"]) == 0
    assert not target.exists()
    assert local.read_bytes() == before[2]
    assert [status for status, _ in responses(root, ["/about/", "/blog/about/", "/blog/"])] == [
        404,
        200,
        200,
    ]
    assert not list(root.rglob("*.sqlite3"))


def test_modular_first_shared_controller_failure_cleans_package(tmp_path, monkeypatch):
    from aurora import _cli_scaffolding as scaffolding

    root = scaffolding.create_project("newsroom", directory=tmp_path, modular=True)
    original = (root / "app.py").read_bytes()

    def fail_replace(*args):
        raise OSError("simulated publication failure")

    monkeypatch.setattr(scaffolding.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated publication failure"):
        scaffolding.create_standalone_controller("About", directory=root)
    assert (root / "app.py").read_bytes() == original
    assert not (root / "controllers").exists()
    assert not list(root.glob(".aurora-controller-*"))


def test_shared_controller_added_before_same_named_local_controller(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["new", "newsroom", "--modular"]) == 0
    root = tmp_path / "newsroom"
    monkeypatch.chdir(root)
    assert main(["controller", "add", "About"]) == 0
    assert main(["controller", "add", "blog", "About"]) == 0
    settings = root / "aurora_config.json"
    config = json.loads(settings.read_text())
    config["strict"] = False
    settings.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["controller", "remove", "About"]) == 0
    assert [status for status, _ in responses(root, ["/about/", "/blog/about/"])] == [404, 200]
