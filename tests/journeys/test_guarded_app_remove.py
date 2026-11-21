"""Developer-facing guardrails for explicitly removing an untouched modular app."""

import json
from pathlib import Path

import pytest

from aurora._cli_scaffolding import add_app, create_project, remove_app


def project(tmp_path):
    root = create_project("newsroom", directory=tmp_path, modular=True)
    add_app("accounts", directory=root)
    return root


def set_strict(root, strict):
    config = root / "aurora_config.json"
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["strict"] = strict
    config.write_text(json.dumps(settings), encoding="utf-8")


def test_refuses_by_default_without_changes(tmp_path, monkeypatch):
    root = project(tmp_path)
    before = (root / "app.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Should not prompt in strict mode"))
    with pytest.raises(ValueError, match="strict"):
        remove_app("accounts", directory=root)
    assert (root / "app.py").read_bytes() == before
    assert (root / "apps/accounts").is_dir()


@pytest.mark.parametrize("answer", ["", "n", "yes", "no"])
def test_cancel_does_not_modify_anything(tmp_path, monkeypatch, answer):
    root = project(tmp_path)
    set_strict(root, False)
    original = (root / "app.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert remove_app("accounts", directory=root) is None
    assert (root / "app.py").read_bytes() == original
    assert (root / "apps/accounts").is_dir()


def test_exact_name_and_successful_guarded_removal(tmp_path, monkeypatch):
    root = project(tmp_path)
    set_strict(root, False)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    with pytest.raises(ValueError, match="lowercase"):
        remove_app("Accounts", directory=root)
    assert remove_app("accounts", directory=root) == root / "apps/accounts"
    assert not (root / "apps/accounts").exists()
    assert (root / "apps/home").exists()
    assert (root / "apps/blog").exists()
    source = (root / "app.py").read_text(encoding="utf-8")
    assert "apps.accounts" not in source
    assert '"/accounts/"' not in source


@pytest.mark.parametrize("kind", ["custom-file", "custom-controller", "external-reference", "custom-route"])
def test_customizations_and_references_refuse_before_prompt(tmp_path, monkeypatch, kind):
    root = project(tmp_path)
    set_strict(root, False)
    app = root / "apps/accounts"
    if kind == "custom-file":
        (app / "custom.py").write_text("important = True\n", encoding="utf-8")
    elif kind == "custom-controller":
        (app / "controllers/Accounts.py").write_text("# customized\n", encoding="utf-8")
    elif kind == "external-reference":
        (root / "models/Remote.py").write_text("from apps.accounts import Accounts\n", encoding="utf-8")
    else:
        file = root / "app.py"
        file.write_text(file.read_text(encoding="utf-8").replace('app.route("/accounts/", Accounts)', 'app.route("/accounts/", Custom)'), encoding="utf-8")
    original = (root / "app.py").read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Should refuse before prompting"))
    with pytest.raises(ValueError):
        remove_app("accounts", directory=root)
    assert app.exists()
    assert (root / "app.py").read_bytes() == original


def test_symlink_in_app_refuses(tmp_path, monkeypatch):
    root = project(tmp_path)
    set_strict(root, False)
    target = tmp_path / "valuable.txt"
    target.write_text("precious", encoding="utf-8")
    try:
        (root / "apps/accounts/views/link.html").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Should refuse before prompting"))
    with pytest.raises(ValueError, match="symbolic link"):
        remove_app("accounts", directory=root)
    assert target.read_text(encoding="utf-8") == "precious"


def test_cli_removal_refuses_strict_and_production(tmp_path, monkeypatch, capsys):
    from aurora.cli import main

    root = project(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("Refusal must not prompt"))
    with pytest.raises(SystemExit) as strict:
        main(["app", "remove", "accounts"])
    assert strict.value.code == 1
    assert "strict" in capsys.readouterr().err

    set_strict(root, False)
    config = root / "aurora_config.json"
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["production"] = True
    config.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(SystemExit) as production:
        main(["app", "remove", "accounts"])
    assert production.value.code == 1
    assert "production" in capsys.readouterr().err
    assert (root / "apps/accounts").is_dir()


def test_cli_removal_works_on_intact_generated_app(tmp_path, monkeypatch, capsys):
    from aurora.cli import main

    root = project(tmp_path)
    set_strict(root, False)
    monkeypatch.chdir(root)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert main(["app", "remove", "accounts"]) == 0
    assert not (root / "apps/accounts").exists()
    assert "database unchanged" in capsys.readouterr().out
